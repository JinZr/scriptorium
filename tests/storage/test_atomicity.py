import os

import pytest

from scriptorium import storage
from scriptorium.domain import AgentRole, AttemptStatus, RunStatus, Task, TaskStatus
from scriptorium.storage import RunStore, StorageError

from ._factories import make_run


class InjectedFailure(Exception):
    pass


def _reviewing_run_with_running_attempt(store: RunStore):
    run = store.create_run(make_run())
    store.update_run(run.id, RunStatus.REVIEWING)
    running = store.create_task(Task(run_id=run.id, stage="review", role=AgentRole.COPYEDIT, input_digest="d"))
    pending = store.create_task(Task(run_id=run.id, stage="review", role=AgentRole.CONSISTENCY, input_digest="e"))
    attempt = store.begin_attempt(running.id, thread_id="session-1")
    return run, running, pending, attempt


def _snapshot(tmp_path, run_id: str) -> tuple[bytes, list[str]]:
    directory = tmp_path / "runs" / run_id
    return (directory / "state.json").read_bytes(), sorted(path.name for path in directory.iterdir())


def test_a_failure_before_the_replace_leaves_the_old_state_whole(tmp_path, monkeypatch) -> None:
    with RunStore(tmp_path) as store:
        run, running, pending, attempt = _reviewing_run_with_running_attempt(store)
        before = _snapshot(tmp_path, run.id)
        events = store.list_events(run.id)

        def fail(source, target):
            raise OSError("disk full")

        monkeypatch.setattr(storage.os, "replace", fail)
        with pytest.raises(StorageError, match="cannot write run state"):
            store.cancel_run(run.id, "stop", "cancel-1")
        monkeypatch.undo()

        assert _snapshot(tmp_path, run.id) == before
        assert store.get_run(run.id).status == RunStatus.REVIEWING
        assert store.get_attempt(attempt.id).status == AttemptStatus.RUNNING
        assert store.get_task(pending.id).status == TaskStatus.PENDING
        assert store.list_events(run.id) == events
        assert store.cancel_run(run.id, "stop", "cancel-2").status == RunStatus.CANCELLED


def test_a_failure_inside_one_operation_writes_nothing(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run, running, pending, attempt = _reviewing_run_with_running_attempt(store)
        before = _snapshot(tmp_path, run.id)

        with pytest.raises(StorageError, match="artifact is not recorded"):
            store.finish_attempt(attempt.id, AttemptStatus.FAILED, validation_report_artifact_digest="f" * 64)
        with pytest.raises(ValueError, match="invalid run status transition"):
            store.update_run(run.id, RunStatus.COMPLETED)

        assert _snapshot(tmp_path, run.id) == before


def test_a_failure_after_the_replace_leaves_the_new_state_complete(tmp_path, monkeypatch) -> None:
    replace = os.replace
    with RunStore(tmp_path) as store:
        run, running, pending, attempt = _reviewing_run_with_running_attempt(store)

        def replace_then_fail(source, target):
            replace(source, target)
            raise InjectedFailure

        monkeypatch.setattr(storage.os, "replace", replace_then_fail)
        with pytest.raises(InjectedFailure):
            store.cancel_run(run.id, "stop", "cancel-1")
        monkeypatch.undo()

    with RunStore(tmp_path) as reopened:
        assert reopened.get_run(run.id).status == RunStatus.CANCELLED
        assert reopened.get_attempt(attempt.id).status == AttemptStatus.INTERRUPTED
        assert reopened.get_task(running.id).status == TaskStatus.CANCELLED
        assert reopened.get_task(pending.id).status == TaskStatus.CANCELLED
        assert sorted(event.event_type for event in reopened.list_events(run.id)[-5:]) == [
            "attempt.interrupted",
            "run.cancelled",
            "run.status_changed",
            "task.status_changed",
            "task.status_changed",
        ]
    assert sorted(path.name for path in (tmp_path / "runs" / run.id).iterdir()) == ["state.json", "state.lock"]
