import asyncio
import os
from pathlib import Path
import threading

import pytest

from scriptorium.domain import AgentRole, RunStatus
from scriptorium.errors import InfrastructureError, NotFoundError, StateError
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository, submit


def _started(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
    return repo, run


def test_read_only_views_leave_an_active_external_attempt_untouched(tmp_path):
    repo, run = _started(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        task = service.database.list_tasks(run.id)[0]
        before = service.database.get_attempt(first["attempt"].id)
        assert service.get_run(run.id)["run"].status == RunStatus.REVIEWING
        assert service.list_tasks(run.id)["tasks"][0]["task"].id == task.id
        assert service.database.get_attempt(first["attempt"].id) == before
        assert len(service.database.list_attempts(task.id)) == 1


def test_cancel_is_durable_and_rejects_a_late_result(tmp_path):
    repo, run = _started(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        cancelled = service.cancel_run(run.id, "User stopped review")
        assert cancelled["run"].status == RunStatus.CANCELLED
        with pytest.raises(StateError, match="no longer active"):
            submit(service, review, {"summary": "Late result.", "findings": []})
        assert service.cancel_run(run.id, "User stopped review")["run"].status == RunStatus.CANCELLED
        events = [event for event in service.database.list_events(run.id) if event.event_type == "run.cancelled"]
        assert len(events) == 1


def test_retry_rejects_a_task_from_another_run(tmp_path):
    repo, first = _started(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        second = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        foreign_task = service.database.list_tasks(second.id)[0]
        with pytest.raises(StateError, match="does not belong"):
            asyncio.run(service.retry_task(first.id, foreign_task.id))
        assert service.database.get_run(first.id).status == RunStatus.REVIEWING


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_run_lock_rejects_links_before_writing_owner_metadata(tmp_path, link_kind):
    repo, run = _started(tmp_path)
    lock_path = repo / ".scriptorium" / "locks" / f"{run.id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / f"{link_kind}-target.txt"
    target.write_text("must remain intact", encoding="utf-8")
    lock_path.unlink()
    if link_kind == "symlink":
        lock_path.symlink_to(target)
    else:
        os.link(target, lock_path)
    with ScriptoriumService(repo) as service:
        with pytest.raises(InfrastructureError):
            service.cancel_run(run.id, "stop")
    assert target.read_text(encoding="utf-8") == "must remain intact"


def test_run_lock_rejects_a_linked_lock_directory(tmp_path):
    repo, run = _started(tmp_path)
    external = tmp_path / "external-locks"
    external.mkdir()
    locks = repo / ".scriptorium" / "locks"
    for path in locks.iterdir():
        path.unlink()
    locks.rmdir()
    locks.symlink_to(external, target_is_directory=True)
    with ScriptoriumService(repo) as service:
        with pytest.raises(InfrastructureError):
            service.cancel_run(run.id, "stop")
    assert list(external.iterdir()) == []


@pytest.mark.parametrize("value", ["../escaped", "/tmp/scriptorium-escaped-run"])
def test_untrusted_run_id_is_resolved_before_a_lock_path_is_created(tmp_path, value):
    repo, _ = _started(tmp_path)
    with ScriptoriumService(repo) as service:
        with pytest.raises(NotFoundError, match="run not found"):
            asyncio.run(service.resume_run(value))
    assert not (repo / ".scriptorium" / "escaped.lock").exists()
    assert not Path(f"{value}.lock").exists()


@pytest.mark.parametrize("exception", [RuntimeError("failed"), asyncio.CancelledError(), KeyboardInterrupt()])
def test_run_operation_releases_lock_for_all_exit_paths(tmp_path, exception):
    repo, run = _started(tmp_path)
    with ScriptoriumService(repo) as service:
        with pytest.raises(type(exception)):
            with service._run_operation(run.id, "run resume"):
                raise exception
        with service._run_operation(run.id, "run retry"):
            pass


def test_cancel_waits_for_in_progress_operation_then_invalidates_claim(tmp_path):
    repo, run = _started(tmp_path)
    finished = threading.Event()
    result = {}
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        with service._run_operation(run.id, "slow compile"):

            def cancel():
                with ScriptoriumService(repo) as contender:
                    result["run"] = contender.cancel_run(run.id, "stop during compile")["run"]
                finished.set()

            worker = threading.Thread(target=cancel)
            worker.start()
            assert not finished.wait(0.1)
            assert service.database.get_attempt(review["attempt"].id).status.value == "running"
        worker.join(timeout=5)
        assert finished.is_set()
        assert result["run"].status == RunStatus.CANCELLED
        assert service.database.get_attempt(review["attempt"].id).status.value == "interrupted"
