import asyncio
import json
import subprocess

import pytest

from scriptorium.domain import AgentRole, AttemptStatus
from scriptorium.errors import ExampleUnavailableError
import scriptorium.schemas
from scriptorium.schemas import (
    InventoriedScientificReviewOutput,
    JudgedScientificReviewOutput,
    RatedReviewOutput,
    RatedScientificReviewOutput,
    ScientificReviewOutput,
    ScopedReviewOutput,
)
from scriptorium.service import ScriptoriumService
from scriptorium.tool_output import MAX_TOOL_RESPONSE_BYTES, success_json
import scriptorium.workflow

from ._support import MANUSCRIPT, PdfBuildingManuscriptManager, claim, make_repository, prepare_patch

_REVIEW_ROLES = ("substantive_review", "copyedit", "consistency", "figure_review")


def _example(service, attempt_id):
    """Follow the example fragments to the end, checking each response stays within the tool limit."""
    pieces, offset, fragments = [], 0, 0
    while offset is not None:
        fragment = service.task_view(service.show_task(attempt_id), "example", offset)
        assert len(success_json(fragment).encode("utf-8")) <= MAX_TOOL_RESPONSE_BYTES
        assert fragment["offset"] == offset
        pieces.append(fragment["text"])
        offset, fragments = fragment["next_offset"], fragments + 1
    return "".join(pieces), fragment["digest"], fragments


def _anchors(value):
    if isinstance(value, dict):
        if "source_digest" in value or "page" in value and "source_path" in value:
            yield value
        for item in value.values():
            yield from _anchors(item)
    elif isinstance(value, list):
        for item in value:
            yield from _anchors(item)


def _frozen_model(service, context):
    run = service.database.get_run(context["run_id"])
    metadata = service.database.get_external_task(context["task"].id)
    return service.armarius._output_model_for_schema(run, metadata["schema_kind"], context["schema"])


@pytest.mark.parametrize("role", _REVIEW_ROLES)
def test_each_review_role_serves_a_checked_example_with_real_frozen_anchors(tmp_path, role):
    repo = make_repository(tmp_path, roles=(role,))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, role)
        overview = service.task_view(context)
        text, digest, fragments = _example(service, context["attempt"].id)
        check = service.check_submission(context["attempt"].id, context["input_digest"], text)
        model = _frozen_model(service, context)

    attempt_id = context["attempt"].id
    assert overview["inputs"]["example"] == {
        "digest": digest,
        "command": f"scriptorium --json task show {attempt_id} --part example",
    }
    assert fragments == 1
    assert check["valid"] is True and check["validation_report"] is None
    example = json.loads(text)
    model.model_validate_json(text)
    sources = {source["source_path"]: source for source in context["source_map"]["sources"]}
    anchors = list(_anchors(example))
    assert {anchor["source_path"] for anchor in anchors} == {"main.tex", "manuscript.pdf"}
    for anchor in anchors:
        if anchor["source_path"] == "manuscript.pdf":
            assert anchor == {"source_path": "manuscript.pdf", "page": 1}
        else:
            assert anchor["source_digest"] == sources["main.tex"]["source_digest"]
            assert (anchor["start_line"], anchor["end_line"], anchor["quoted_text"]) == (
                1,
                1,
                MANUSCRIPT.splitlines()[0],
            )
    finding = example["findings"][0]
    assert {"consequence", "evidence"} <= set(finding)
    assert ("affected_claim" in finding) == (role != "substantive_review")
    assert example["scope"]["outstanding"] and example["scope"]["limitations"]
    if role == "substantive_review":
        assert example["verdict"]["recommendation"] == "major_revision"
        assert [entry["prominence"] for entry in example["claim_inventory"]] == ["headline", "supporting"]
        checks = example["claim_checks"]
        assert [check["claim_index"] for check in checks] == [0, 1]
        assert {check["assessment"] for check in checks} == {"finding", "supported"}
        assert any("recomputation" in check for check in checks)


def test_a_finished_attempt_still_serves_its_example(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, "copyedit")
        text, digest, _ = _example(service, context["attempt"].id)
        receipt = asyncio.run(service.submit_task(context["attempt"].id, context["input_digest"], text))
        assert receipt["attempt"].status == AttemptStatus.COMPLETED

        assert _example(service, context["attempt"].id)[:2] == (text, digest)


def test_a_long_example_is_read_through_bounded_fragments(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    long_line = "% " + "x" * 3000
    (repo / "main.tex").write_text(f"{long_line}\n{MANUSCRIPT}", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-am", "Long first line"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, "substantive_review")
        text, _, fragments = _example(service, context["attempt"].id)
        check = service.check_submission(context["attempt"].id, context["input_digest"], text)

    assert len(text.encode("utf-8")) > MAX_TOOL_RESPONSE_BYTES and fragments > 1
    assert json.loads(text)["findings"][0]["evidence"][0]["quoted_text"] == long_line
    assert check["valid"] is True


@pytest.mark.parametrize(
    "models",
    [
        {"review": ScopedReviewOutput, "scientific_review": ScientificReviewOutput},
        {"review": ScopedReviewOutput, "scientific_review": JudgedScientificReviewOutput},
        {"review": ScopedReviewOutput, "scientific_review": InventoriedScientificReviewOutput},
        {"review": RatedReviewOutput, "scientific_review": RatedScientificReviewOutput},
    ],
)
def test_runs_frozen_with_an_older_schema_shape_get_an_example_of_that_shape(tmp_path, monkeypatch, models):
    repo = make_repository(tmp_path, roles=("substantive_review", "copyedit"))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        with monkeypatch.context() as patch:
            for kind, model in models.items():
                patch.setitem(scriptorium.schemas.SCHEMA_MODELS, kind, model)
            run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        for role, kind in ((AgentRole.SUBSTANTIVE_REVIEW, "scientific_review"), (AgentRole.COPYEDIT, "review")):
            context = claim(service, run.id, role)
            text, _, _ = _example(service, context["attempt"].id)
            check = service.check_submission(context["attempt"].id, context["input_digest"], text)
            assert _frozen_model(service, context) is models[kind]
            assert check["valid"] is True, check["validation_report"]
            published = set(context["schema"]["properties"])
            assert set(json.loads(text)) == published


def test_a_shape_the_builder_cannot_fill_gets_a_structured_error(tmp_path, monkeypatch):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, "copyedit")
        monkeypatch.setattr(scriptorium.workflow, "build_review_example", lambda *args: {"summary": "", "findings": []})
        overview = service.task_view(service.show_task(context["attempt"].id))
        with pytest.raises(ExampleUnavailableError, match="no example is available .* review output schema shape"):
            service.task_view(service.show_task(context["attempt"].id), "example")

    assert "example" not in overview["inputs"]
    assert ExampleUnavailableError.code == "example_unavailable"


def test_revision_attempts_report_that_no_example_is_available(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        prepare_patch(service, run.id)
        revision = next(task for task in service.database.list_tasks(run.id) if task.role == AgentRole.REVISION)
        attempt_id = service.database.list_attempts(revision.id)[-1].id
        overview = service.task_view(service.show_task(attempt_id))
        with pytest.raises(ExampleUnavailableError, match="frozen revision output schema shape"):
            service.task_view(service.show_task(attempt_id), "example")

    assert "example" not in overview["inputs"]
