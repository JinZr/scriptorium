import asyncio
import json
import subprocess

import pytest

from scriptorium.domain import AgentRole, AttemptStatus
from scriptorium.errors import InfrastructureError
from scriptorium.schemas import SCHEMA_MODELS
from scriptorium.service import ScriptoriumService
import scriptorium.workflow

from ._support import MANUSCRIPT, PdfBuildingManuscriptManager, claim, make_repository, prepare_patch

_REVIEW_ROLES = ("substantive_review", "copyedit", "consistency", "figure_review")


def _example(service, attempt_id, directory):
    """Export the attempt and return its example text and reported digest, or None when none was frozen."""
    exported = service.export_task(attempt_id, directory)
    digests = {item["path"]: item["digest"] for item in exported["task_inputs"]}
    if "task/example.json" not in digests:
        assert not (directory / "task/example.json").exists()
        return None
    return (directory / "task/example.json").read_bytes().decode("utf-8"), digests["task/example.json"]


def _anchors(value):
    if isinstance(value, dict):
        if "source_digest" in value or "page" in value and "source_path" in value:
            yield value
        for item in value.values():
            yield from _anchors(item)
    elif isinstance(value, list):
        for item in value:
            yield from _anchors(item)


def _output_model(service, context):
    return SCHEMA_MODELS[service.store.get_external_task(context["task"].id)["schema_kind"]]


@pytest.mark.parametrize("role", _REVIEW_ROLES)
def test_each_review_role_serves_a_checked_example_with_real_frozen_anchors(tmp_path, role):
    repo = make_repository(tmp_path, roles=(role,))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, role)
        text, digest = _example(service, context["attempt"].id, tmp_path / "export")
        check = service.check_submission(context["attempt"].id, context["input_digest"], text)
        model = _output_model(service, context)
        frozen = service.store.get_external_task(context["task"].id)["example_digest"]
        stored = service.armarius.artifacts.get_bytes(frozen).decode("utf-8")

    assert (stored, frozen) == (text, digest)
    assert check["valid"] is True and check["validation_report"] is None
    example = json.loads(text)
    model.model_validate_json(text)
    sources = {source["source_path"]: source for source in context["source_map"]["sources"]}
    anchors = list(_anchors(example))
    cited = {"main.tex", "manuscript.pdf"} if role == "substantive_review" else {"main.tex"}
    assert {anchor["source_path"] for anchor in anchors} == cited
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
    assert "scope" not in example and example["limitations"]
    if role == "substantive_review":
        assert example["verdict"]["recommendation"] == "major_revision"
        assert [entry["prominence"] for entry in example["claim_inventory"]] == ["headline", "supporting"]
        checks = example["claim_checks"]
        assert [check["claim_index"] for check in checks] == [0, 1]
        assert {check["assessment"] for check in checks} == {"finding", "supported"}
        assert any("recomputation" in check for check in checks)


def test_a_finished_attempt_still_exports_its_example(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, "copyedit")
        text, digest = _example(service, context["attempt"].id, tmp_path / "active")
        receipt = asyncio.run(service.submit_task(context["attempt"].id, context["input_digest"], text))
        assert receipt["attempt"].status == AttemptStatus.COMPLETED

        assert _example(service, context["attempt"].id, tmp_path / "completed") == (text, digest)


def test_a_long_example_is_exported_whole(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    long_line = "% " + "x" * 3000
    (repo / "main.tex").write_text(f"{long_line}\n{MANUSCRIPT}", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-am", "Long first line"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, "substantive_review")
        text, _ = _example(service, context["attempt"].id, tmp_path / "export")
        check = service.check_submission(context["attempt"].id, context["input_digest"], text)

    assert json.loads(text)["findings"][0]["evidence"][0]["quoted_text"] == long_line
    assert check["valid"] is True


def test_an_example_that_fails_validation_is_not_frozen(tmp_path, monkeypatch):
    repo = make_repository(tmp_path, roles=("copyedit",))
    monkeypatch.setattr(scriptorium.workflow, "build_review_example", lambda *args: {"summary": "", "findings": []})
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, "copyedit")
        assert _example(service, context["attempt"].id, tmp_path / "export") is None
        assert service.store.get_external_task(context["task"].id)["example_digest"] is None


def test_revision_attempts_export_no_example(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        prepare_patch(service, run.id)
        revision = next(task for task in service.store.list_tasks(run.id) if task.role == AgentRole.REVISION)
        attempt_id = service.store.list_attempts(revision.id)[-1].id
        assert _example(service, attempt_id, tmp_path / "export") is None
        assert service.store.get_external_task(revision.id)["example_digest"] is None


def test_a_tampered_snapshot_fails_example_preparation_as_infrastructure(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        armarius = service.armarius
        bundle = armarius._bundle_for_run(run)
        assert armarius._output_example(run, AgentRole.COPYEDIT, "review", bundle) is not None
        snapshot_source = armarius._run_dir(run.id) / "snapshot" / "main.tex"
        snapshot_source.chmod(0o644)
        snapshot_source.write_text(MANUSCRIPT.replace("article", "report"), encoding="utf-8")

        # Preparing a task, as resume does for a run interrupted before its review tasks, rereads the snapshot.
        with pytest.raises(InfrastructureError, match="frozen source does not match its source digest: main.tex"):
            armarius._output_example(run, AgentRole.COPYEDIT, "review", bundle)
