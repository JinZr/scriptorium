import asyncio
import os
from pathlib import Path

import pytest

from scriptorium.artifacts import ArtifactStore
from scriptorium.domain import AgentRole
from scriptorium.errors import ConfigurationError, InfrastructureError, StateError
from scriptorium.service import ScriptoriumService
from scriptorium.tool_output import MAX_TOOL_RESPONSE_BYTES, bound_export, fits_response

from ._support import PdfBuildingManuscriptManager, claim, complete_reviews, make_repository, submit

EXPECTED = {"manifest.json", "manuscript.pdf", "navigation.json", "source-map.json", "sources/main.tex"}


@pytest.fixture
def exporting(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        context = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        yield service, run, context, tmp_path / "out"


def _exports(service, run):
    return [event for event in service.database.list_events(run.id) if event.event_type == "tool.export"]


def _files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def test_export_matches_the_frozen_bundle_and_is_recorded_for_the_attempt(exporting):
    service, run, context, out = exporting
    response = service.export_task(context["attempt"].id, out)
    assert _files(out) == EXPECTED
    assert {item["path"] for item in response["files"]} == EXPECTED
    for item in response["files"]:
        assert (out / item["path"]).read_bytes() == (Path(context["bundle_path"]) / item["path"]).read_bytes()
        assert ArtifactStore.digest_file(out / item["path"]) == item["digest"]
        assert (out / item["path"]).stat().st_mode & 0o777 == 0o444
    assert not any(path.name.startswith("page") for path in out.rglob("*"))
    (event,) = _exports(service, run)
    assert (event.entity_type, event.entity_id) == ("attempt", context["attempt"].id)
    assert event.payload["files"] == sorted(response["files"], key=lambda item: item["path"])
    assert event.payload["bundle_digest"] == context["attempt"].bundle_digest
    assert response["files_digest"] and "only task command" in response["note"]
    assert fits_response(response)


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
    assert _files(out) == EXPECTED and len(_exports(service, run)) == 1


def test_an_unrecordable_export_is_rolled_back(exporting, monkeypatch):
    service, run, context, out = exporting

    def refuse(*arguments):
        raise StateError("cannot record")

    monkeypatch.setattr(service, "_record_access", refuse)
    with pytest.raises(StateError, match="cannot record"):
        service.export_task(context["attempt"].id, out)
    assert not out.exists()


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


def test_a_finished_attempt_cannot_export(exporting):
    service, run, context, out = exporting
    submit(service, context, {"summary": "Done.", "findings": []})
    with pytest.raises(StateError, match="active attempt"):
        service.export_task(context["attempt"].id, out)
    assert not out.exists() and _exports(service, run) == []


def test_a_revision_attempt_exports_like_task_read_allows(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        complete_reviews(service, run.id)
        service.decide_finding(service.list_findings(run.id)[0].id, "confirm", "Correct it.")
        asyncio.run(service.resume_run(run.id))
        revision = claim(service, run.id, AgentRole.REVISION, session="revision-session")
        response = service.export_task(revision["attempt"].id, tmp_path / "revision-out")
        assert {item["path"] for item in response["files"]} == EXPECTED
        assert len(_exports(service, run)) == 1


def test_the_response_stays_bounded_for_many_files():
    listing = [{"path": f"sources/chapters/section-{number:04d}.tex", "digest": "a" * 64} for number in range(400)]
    response = bound_export("attempt_1", "/exports/" + "x" * 200, listing, 123456)
    assert fits_response(response)
    assert response["file_count"] == 400 and response["listing_truncated"] is True
    assert 0 < len(response["files"]) < 400
    assert response["files"] == listing[: len(response["files"])]
    assert len(response["note"].encode()) < MAX_TOOL_RESPONSE_BYTES
    small = bound_export("attempt_1", "/exports/a", listing[:3], 10)
    assert small["files"] == listing[:3] and small["listing_truncated"] is False
