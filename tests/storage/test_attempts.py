import pytest

from scriptorium.domain import AgentRole, Artifact, AttemptStatus, RunStatus, Task, TaskStatus
from scriptorium.storage import ConflictError, RunStore, StorageError

from ._factories import create_completed_attempt, make_run


def test_run_task_attempt_lifecycle_persists(tmp_path) -> None:
    path = tmp_path
    with RunStore(path) as store:
        run = store.create_run(make_run())
        store.update_run(run.id, RunStatus.REVIEWING)
        task, attempt = create_completed_attempt(store, run)

        assert attempt.status == AttemptStatus.COMPLETED
        assert attempt.thread_id == "thread-1"
        assert store.get_task(task.id).status == TaskStatus.COMPLETED
        assert [event.event_type for event in store.list_events(run.id)] == [
            "run.created",
            "run.status_changed",
            "task.created",
            "attempt.started",
            "attempt.finished",
        ]
        with pytest.raises(ConflictError, match="already terminal"):
            store.update_attempt(attempt.id, AttemptStatus.FAILED)

    with RunStore(path) as reopened:
        assert reopened.get_run(run.id).status == RunStatus.REVIEWING
        assert reopened.get_task(task.id).status == TaskStatus.COMPLETED
        assert reopened.list_attempts(task.id)[0].thread_id == "thread-1"


def test_attempt_retry_is_append_only(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        task = store.create_task(
            Task(
                run_id=run.id,
                stage="review",
                role=AgentRole.CONSISTENCY,
                input_digest="d" * 64,
            )
        )
        first = store.begin_attempt(task.id)
        assert store.recover_orphaned_attempts(run.id) == 1
        assert store.get_task(task.id).status == TaskStatus.INTERRUPTED
        interrupted = store.list_events(run.id)[-1]
        assert interrupted.event_type == "attempt.interrupted"
        assert interrupted.entity_id == first.id
        assert interrupted.payload == {"task_id": task.id, "ordinal": 1}
        events = store.list_events(run.id)
        assert store.recover_orphaned_attempts(run.id) == 0
        assert store.list_events(run.id) == events
        second = store.begin_attempt(task.id)

        assert first.ordinal == 1
        assert second.ordinal == 2
        assert [attempt.status for attempt in store.list_attempts(task.id)] == [
            AttemptStatus.INTERRUPTED,
            AttemptStatus.RUNNING,
        ]
        with pytest.raises(ConflictError, match="already terminal"):
            store.finish_attempt(first.id, AttemptStatus.COMPLETED)
    with RunStore(tmp_path) as reopened:
        assert [attempt.id for attempt in reopened.list_attempts(task.id)] == [first.id, second.id]


def test_attempt_session_is_recorded_once_while_running(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        task = store.create_task(
            Task(
                run_id=run.id,
                stage="review",
                role=AgentRole.CONSISTENCY,
                input_digest="d" * 64,
            )
        )
        attempt = store.begin_attempt(task.id)

        recorded = store.record_attempt_session(attempt.id, "session-1")
        replayed = store.record_attempt_session(attempt.id, "session-1")

        assert recorded.thread_id == "session-1"
        assert replayed == recorded
        with pytest.raises(ConflictError, match="different session"):
            store.record_attempt_session(attempt.id, "session-2")

        finished = store.finish_attempt(
            attempt.id,
            AttemptStatus.INTERRUPTED,
            thread_id="session-1",
        )
        assert finished.thread_id == "session-1"
        with pytest.raises(ConflictError, match="already terminal"):
            store.record_attempt_session(attempt.id, "session-1")


def test_finish_attempt_advances_the_run_updated_at(tmp_path, monkeypatch) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        task = store.create_task(
            Task(
                run_id=run.id,
                stage="review",
                role=AgentRole.CONSISTENCY,
                input_digest="d" * 64,
            )
        )
        attempt = store.begin_attempt(task.id)
        finished_at = "2999-01-01T00:00:00+00:00"
        monkeypatch.setattr("scriptorium.storage.utc_now", lambda: finished_at)

        finished = store.finish_attempt(attempt.id, AttemptStatus.COMPLETED)

        assert finished.completed_at == finished_at
        assert store.get_run(run.id).updated_at == finished_at

    with RunStore(tmp_path) as reopened:
        assert reopened.get_run(run.id).updated_at == finished_at


def test_finish_attempt_rejects_a_different_recorded_session(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        task = store.create_task(
            Task(
                run_id=run.id,
                stage="review",
                role=AgentRole.CONSISTENCY,
                input_digest="d" * 64,
            )
        )
        attempt = store.begin_attempt(task.id, thread_id="session-1")

        with pytest.raises(ConflictError, match="different session"):
            store.finish_attempt(
                attempt.id,
                AttemptStatus.INTERRUPTED,
                thread_id="session-2",
            )

        assert store.get_attempt(attempt.id).status == AttemptStatus.RUNNING


def test_validation_report_pointer_is_atomic_and_persists(tmp_path) -> None:
    path = tmp_path
    digest = "f" * 64
    with RunStore(path) as store:
        run = store.create_run(make_run())
        task = store.create_task(
            Task(
                run_id=run.id,
                stage="review",
                role=AgentRole.CONSISTENCY,
                input_digest="d" * 64,
            )
        )
        attempt = store.begin_attempt(task.id)
        events_before = store.list_events(run.id)

        with pytest.raises(StorageError, match="artifact is not recorded"):
            store.finish_attempt(
                attempt.id,
                AttemptStatus.FAILED,
                validation_report_artifact_digest=digest,
            )

        assert store.get_attempt(attempt.id).status == AttemptStatus.RUNNING
        assert store.list_events(run.id) == events_before

        store.record_artifact(
            Artifact(
                digest=digest,
                relative_path=f"sha256/{digest[:2]}/{digest[2:]}",
                size=2,
                media_type="application/vnd.scriptorium.validation-report+json",
            )
        )
        finished = store.finish_attempt(
            attempt.id,
            AttemptStatus.FAILED,
            validation_report_artifact_digest=digest,
        )
        assert finished.validation_report_artifact_digest == digest

    with RunStore(path) as reopened:
        assert reopened.get_attempt(attempt.id).validation_report_artifact_digest == digest
        with pytest.raises(ConflictError, match="already terminal"):
            reopened.finish_attempt(attempt.id, AttemptStatus.COMPLETED)
        assert reopened.get_attempt(attempt.id).validation_report_artifact_digest == digest


def test_cancel_incomplete_tasks_preserves_completed_history(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        completed, _ = create_completed_attempt(store, run)
        pending = store.create_task(
            Task(
                run_id=run.id,
                stage="review",
                role=AgentRole.CONSISTENCY,
                input_digest="e" * 64,
            )
        )

        assert store.cancel_incomplete_tasks(run.id) == 1
        assert store.get_task(completed.id).status == TaskStatus.COMPLETED
        assert store.get_task(pending.id).status == TaskStatus.CANCELLED


def test_durable_cancel_is_atomic_and_idempotent_by_terminal_state(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        store.update_run(run.id, RunStatus.REVIEWING)
        task = store.create_task(
            Task(
                run_id=run.id,
                stage="review",
                role=AgentRole.CONSISTENCY,
                input_digest="d" * 64,
            )
        )
        attempt = store.begin_attempt(task.id, thread_id="session-1")

        cancelled = store.cancel_run(run.id, "stop", "cancel-1")
        replayed = store.cancel_run(run.id, "ignored replay", "cancel-2")

        assert cancelled.status == RunStatus.CANCELLED
        assert replayed == cancelled
        assert store.get_attempt(attempt.id).status == AttemptStatus.INTERRUPTED
        assert store.get_task(task.id).status == TaskStatus.CANCELLED
        events = store.list_events(run.id)
        cancellation_events = [event for event in events if event.event_type == "run.cancelled"]
        assert len(cancellation_events) == 1
        assert cancellation_events[0].payload == {
            "reason": "stop",
            "request_id": "cancel-1",
        }
