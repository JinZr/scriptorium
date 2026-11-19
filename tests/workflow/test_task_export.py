import asyncio
import json
import os
from pathlib import Path
import subprocess

import pytest

from scriptorium.artifacts import ArtifactStore
from scriptorium.domain import AgentRole, digest_json
from scriptorium.errors import ConfigurationError, InfrastructureError, StateError
from scriptorium.service import ScriptoriumService

from ._support import (
    PdfBuildingManuscriptManager,
    approve_plan,
    claim,
    complete_reviews,
    export_writing,
    make_repository,
    prepare_verification,
    submit,
)

BUNDLE = {"manifest.json", "manuscript.pdf", "navigation.json", "source-map.json", "sources/main.tex"}
TASK_INPUTS = {"task/prompt.md", "task/schema.json", "task/example.json"}


@pytest.fixture
def exporting(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        yield service, run, context, tmp_path / "out"


def _exports(service, run):
    return [event for event in service.store.list_events(run.id) if event.event_type == "tool.export"]


def _files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def test_export_writes_the_bundle_and_task_inputs_and_records_them(exporting):
    service, run, context, out = exporting
    attempt = context["attempt"]
    response = service.export_task(attempt.id, out)
    assert _files(out) == BUNDLE | TASK_INPUTS
    assert set(response) == {"attempt_id", "directory", "file_count", "total_bytes", "files_digest", "task_inputs"}
    assert (response["attempt_id"], response["directory"]) == (attempt.id, str(out.resolve()))
    assert response["file_count"] == len(BUNDLE | TASK_INPUTS)
    assert response["total_bytes"] == sum((out / name).stat().st_size for name in BUNDLE | TASK_INPUTS)
    for name in BUNDLE:
        assert (out / name).read_bytes() == (Path(context["bundle_path"]) / name).read_bytes()
    example_digest = service.store.get_external_task(context["task"].id)["example_digest"]
    assert response["task_inputs"] == [
        {"path": "task/prompt.md", "digest": attempt.prompt_digest},
        {"path": "task/schema.json", "digest": attempt.schema_digest},
        {"path": "task/example.json", "digest": example_digest},
    ]
    for item in response["task_inputs"]:
        assert (out / item["path"]).read_bytes() == service.artifacts.get_bytes(item["digest"])
        assert ArtifactStore.digest_file(out / item["path"]) == item["digest"]
    assert (out / "task/prompt.md").read_text(encoding="utf-8") == context["prompt"]
    assert json.loads((out / "task/schema.json").read_text(encoding="utf-8")) == context["schema"]
    for name in BUNDLE | TASK_INPUTS:
        assert (out / name).stat().st_mode & 0o777 == 0o444
    assert not any(path.name.startswith("page") for path in out.rglob("*"))
    (event,) = _exports(service, run)
    assert (event.entity_type, event.entity_id) == ("attempt", attempt.id)
    listing = event.payload["files"]
    assert [item["path"] for item in listing] == sorted(BUNDLE | TASK_INPUTS)
    assert all(ArtifactStore.digest_file(out / item["path"]) == item["digest"] for item in listing)
    assert response["files_digest"] == digest_json(listing)
    assert event.payload["bundle_digest"] == attempt.bundle_digest


def test_a_briefed_run_exports_its_brief(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit",))
    brief = json.dumps({"venue_family": "other", "stage": "internal_draft"})
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick", brief=brief))["run"]
        context = claim(service, run.id, "copyedit")
        response = service.export_task(context["attempt"].id, tmp_path / "out")
        frozen = service.artifacts.get_bytes(run.brief_digest)
    assert {"path": "task/brief.json", "digest": run.brief_digest} in response["task_inputs"]
    assert (tmp_path / "out/task/brief.json").read_bytes() == frozen


def test_a_retried_attempt_exports_its_own_prompt_with_the_correction(exporting, tmp_path):
    service, run, context, out = exporting
    rejected = asyncio.run(service.submit_task(context["attempt"].id, context["input_digest"], "{}"))
    assert rejected["attempt"].status.value == "failed"
    asyncio.run(service.retry_task(run.id, context["task"].id))
    retried = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="second")
    first = service.export_task(context["attempt"].id, tmp_path / "first")
    second = service.export_task(retried["attempt"].id, tmp_path / "second")
    assert first["task_inputs"][0]["digest"] != second["task_inputs"][0]["digest"]
    assert "was rejected" not in (tmp_path / "first/task/prompt.md").read_text(encoding="utf-8")
    assert "was rejected" in (tmp_path / "second/task/prompt.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_a_finished_attempt_exports_without_changing_state(exporting, status):
    service, run, context, out = exporting
    attempt_id = context["attempt"].id
    if status == "interrupted":
        service.cancel_run(run.id, "Stop the review.")
    else:
        submit(service, context, {"summary": "Done.", "findings": []} if status == "completed" else {})
    before = (service.store.get_attempt(attempt_id), service.store.get_task(context["task"].id))
    assert before[0].status.value == status
    response = service.export_task(attempt_id, out)
    assert _files(out) == BUNDLE | TASK_INPUTS and response["file_count"] == len(BUNDLE | TASK_INPUTS)
    assert (service.store.get_attempt(attempt_id), service.store.get_task(context["task"].id)) == before
    assert len(_exports(service, run)) == 1


def test_a_failure_part_way_leaves_no_files_and_no_event(exporting, monkeypatch):
    service, run, context, out = exporting
    real_replace = os.replace
    calls = []

    def failing_replace(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError("disk full")
        real_replace(source, target)

    monkeypatch.setattr("scriptorium.manuscript.os.replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        service.export_task(context["attempt"].id, out)
    assert len(calls) == 2 and not out.exists() and _exports(service, run) == []
    monkeypatch.undo()
    service.export_task(context["attempt"].id, out)
    assert _files(out) == BUNDLE | TASK_INPUTS and len(_exports(service, run)) == 1


def test_an_unrecordable_export_is_rolled_back(exporting, monkeypatch):
    service, run, context, out = exporting

    def refuse(*arguments):
        raise StateError("cannot record")

    monkeypatch.setattr(service, "_record_access", refuse)
    with pytest.raises(StateError, match="cannot record"):
        service.export_task(context["attempt"].id, out)
    assert not out.exists()


def test_a_task_input_that_does_not_match_its_digest_is_never_exported(exporting, monkeypatch):
    service, run, context, out = exporting
    monkeypatch.setattr(service.armarius, "_load_prompt_artifact", lambda digest: "a different prompt")
    with pytest.raises(InfrastructureError, match="does not match its recorded digest: task/prompt.md"):
        service.export_task(context["attempt"].id, out)
    assert not out.exists() and _exports(service, run) == []


@pytest.mark.parametrize("colliding", ["task/prompt.md", "task", "task/notes.md"])
def test_a_bundle_path_colliding_with_the_task_inputs_fails_cleanly(exporting, monkeypatch, colliding):
    service, run, context, out = exporting
    monkeypatch.setattr(service, "_export_names", lambda bundle, files: ["sources/main.tex", colliding])
    with pytest.raises(StateError, match=f"collides with the exported task inputs: {colliding}"):
        service.export_task(context["attempt"].id, out)
    assert not out.exists() and _exports(service, run) == []


def test_a_path_that_would_escape_the_directory_is_refused_before_anything_is_written(exporting, monkeypatch):
    service, run, context, out = exporting
    real = service.armarius._retrieval_bundle
    record = {"path": "../escape.tex", "digest": "0" * 64, "size": 0}

    def tampered(run, metadata):
        bundle, files = real(run, metadata)
        return bundle, {**files, record["path"]: record}

    monkeypatch.setattr(service.armarius, "_retrieval_bundle", tampered)
    monkeypatch.setattr(service, "_export_names", lambda bundle, files: ["sources/main.tex", record["path"]])
    with pytest.raises(StateError, match="safe relative path"):
        service.export_task(context["attempt"].id, out)
    assert not out.exists() and not (out.parent / "escape.tex").exists() and _exports(service, run) == []


def test_a_source_missing_from_the_frozen_index_is_an_infrastructure_failure(exporting, monkeypatch):
    service, run, context, out = exporting
    real = service.armarius._retrieval_bundle

    def without_source(run, metadata):
        bundle, files = real(run, metadata)
        return bundle, {name: item for name, item in files.items() if name != "sources/main.tex"}

    monkeypatch.setattr(service.armarius, "_retrieval_bundle", without_source)
    with pytest.raises(InfrastructureError, match="missing from the frozen bundle index"):
        service.export_task(context["attempt"].id, out)
    assert not out.exists() and _exports(service, run) == []


def test_a_damaged_frozen_file_is_never_exported(exporting):
    service, run, context, out = exporting
    (Path(context["bundle_path"]) / "sources/main.tex").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(InfrastructureError):
        service.export_task(context["attempt"].id, out)
    assert not out.exists() and _exports(service, run) == []


def test_a_used_destination_is_refused_without_recording(exporting):
    service, run, context, out = exporting
    out.mkdir()
    (out / "mine.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="empty directory"):
        service.export_task(context["attempt"].id, out)
    assert [path.name for path in out.iterdir()] == ["mine.txt"] and _exports(service, run) == []


def test_a_writing_attempt_exports_its_plan_assignment_and_writable_passages(tmp_path):
    repo = make_repository(tmp_path)
    (repo / "main.tex").write_bytes(
        b"\\documentclass{article}\r\n\\begin{document}\r\nThe result is teh clear.\r\nEnd."
    )
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "CRLF lines"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        complete_reviews(service, run.id)
        finding_id = service.list_findings(run.id)[0].id
        service.decide_finding(finding_id, "confirm", "Correct it.")
        asyncio.run(service.resume_run(run.id))
        approve_plan(
            service,
            run.id,
            {"result": (["main.tex:3-3", "main.tex:4-4"], [finding_id], "Correct the typo.")},
            plan_text="# Story\n\nThe result is clear.\n",
        )
        writing, out = export_writing(service, run.id)
        passages = {"task/passages/1.tex", "task/passages/2.tex"}
        assert _files(out) == BUNDLE | {"task/prompt.md", "task/plan.md", "task/assignment.md"} | passages
        # The passage holds the exact frozen bytes, its CRLF included; the last line of the file has none.
        assert (out / "task/passages/1.tex").read_bytes() == b"The result is teh clear.\r\n"
        assert (out / "task/passages/2.tex").read_bytes() == b"End."
        assert (out / "task/plan.md").read_text(encoding="utf-8") == "# Story\n\nThe result is clear.\n"
        assert "Correct the typo." in (out / "task/assignment.md").read_text(encoding="utf-8")
        modes = {name: (out / name).stat().st_mode & 0o777 for name in _files(out)}
        assert {name for name, mode in modes.items() if mode == 0o644} == passages
        assert all(mode == 0o444 for name, mode in modes.items() if name not in passages)
        prompt = (out / "task/prompt.md").read_text(encoding="utf-8")
        assert "The result is clear." in prompt and finding_id in prompt and "task/passages/1.tex" in prompt
        recorded = _exports(service, run)[-1].payload["files"]
        assert {item["path"] for item in recorded} == _files(out)


def test_a_verification_attempt_exports_the_patched_bundle(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        prepare_verification(service, run.id)
        verification = claim(service, run.id, AgentRole.VERIFICATION, session="verification-session")
        out = tmp_path / "verification-out"
        service.export_task(verification["attempt"].id, out)
        assert (out / "sources/main.tex").read_bytes() == (
            Path(verification["bundle_path"]) / "sources/main.tex"
        ).read_bytes()
        assert "The result is clear." in (out / "sources/main.tex").read_text(encoding="utf-8")
        assert "Approved diff" in (out / "task/prompt.md").read_text(encoding="utf-8")


def test_claim_and_show_return_a_compact_overview_that_points_to_export(exporting):
    service, _, context, _ = exporting
    attempt_id = context["attempt"].id
    for overview in (service.task_view(context), service.task_view(service.show_task(attempt_id))):
        assert set(overview) == {
            "run_id",
            "task",
            "attempt",
            "input_digest",
            "bundle_digest",
            "navigation_digest",
            "export_command",
        }
        assert overview["input_digest"] == context["input_digest"]
        assert overview["export_command"] == f"scriptorium --json task export {attempt_id} --dir DIR"
