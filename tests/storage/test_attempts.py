import sqlite3

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus, Task, TaskStatus
from scriptorium.storage import ConflictError, Database

from ._factories import create_completed_attempt, make_run


def test_run_task_attempt_lifecycle_persists(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    with Database(path) as database:
        run = database.create_run(make_run())
        database.update_run(run.id, RunStatus.REVIEWING)
        task, attempt = create_completed_attempt(database, run)

        assert attempt.status == AttemptStatus.COMPLETED
        assert attempt.thread_id == "thread-1"
        assert database.get_task(task.id).status == TaskStatus.COMPLETED
        assert database.get_run(run.id).estimated_cost_usd == pytest.approx(0.25)
        assert [event.event_type for event in database.list_events(run.id)] == [
            "run.created",
            "run.status_changed",
            "task.created",
            "attempt.started",
            "attempt.finished",
        ]
        with pytest.raises(ConflictError, match="already terminal"):
            database.update_attempt(attempt.id, AttemptStatus.FAILED)

    with Database(path) as reopened:
        assert reopened.get_run(run.id).status == RunStatus.REVIEWING
        assert reopened.get_task(task.id).status == TaskStatus.COMPLETED
        assert reopened.list_attempts(task.id)[0].thread_id == "thread-1"


def test_attempt_retry_is_append_only(tmp_path) -> None:
    with Database(tmp_path / "state.sqlite3") as database:
        run = database.create_run(make_run())
        task = database.create_task(
            Task(
                run_id=run.id,
                stage="review",
                role=AgentRole.CONSISTENCY,
                route="primary",
                input_digest="d" * 64,
            )
        )
        first = database.begin_attempt(task.id)
        assert database.recover_orphaned_attempts(run.id) == 1
        assert database.get_task(task.id).status == TaskStatus.INTERRUPTED
        interrupted = database.list_events(run.id)[-1]
        assert interrupted.event_type == "attempt.interrupted"
        assert interrupted.entity_id == first.id
        assert interrupted.payload == {"task_id": task.id, "ordinal": 1}
        events = database.list_events(run.id)
        assert database.recover_orphaned_attempts(run.id) == 0
        assert database.list_events(run.id) == events
        second = database.begin_attempt(task.id)

        assert first.ordinal == 1
        assert second.ordinal == 2
        assert [attempt.status for attempt in database.list_attempts(task.id)] == [
            AttemptStatus.INTERRUPTED,
            AttemptStatus.RUNNING,
        ]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.connection.execute("DELETE FROM attempts WHERE id = ?", (first.id,))


def test_cancel_incomplete_tasks_preserves_completed_history(tmp_path) -> None:
    with Database(tmp_path / "state.sqlite3") as database:
        run = database.create_run(make_run())
        completed, _ = create_completed_attempt(database, run)
        pending = database.create_task(
            Task(
                run_id=run.id,
                stage="review",
                role=AgentRole.CONSISTENCY,
                route="primary",
                input_digest="e" * 64,
            )
        )

        assert database.cancel_incomplete_tasks(run.id) == 1
        assert database.get_task(completed.id).status == TaskStatus.COMPLETED
        assert database.get_task(pending.id).status == TaskStatus.CANCELLED
