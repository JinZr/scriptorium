from dataclasses import replace

from scriptorium.domain import AgentRole, RunStatus, Task, TaskStatus
from scriptorium.storage import Database

from ._factories import make_run


def _run(database: Database, created_at: str, status: RunStatus = RunStatus.PREPARING):
    return database.create_run(replace(make_run(), created_at=created_at, updated_at=created_at, status=status))


def test_summaries_list_newest_first_with_total_and_task_counts(tmp_path) -> None:
    with Database(tmp_path / "state.sqlite3") as database:
        oldest = _run(database, "2026-01-01T00:00:00+00:00")
        newest = _run(database, "2026-03-01T00:00:00+00:00", RunStatus.REVIEWING)
        middle = _run(database, "2026-02-01T00:00:00+00:00")
        for index, status in enumerate((TaskStatus.PENDING, TaskStatus.PENDING, TaskStatus.RUNNING)):
            task = Task(run_id=newest.id, stage="review", role=AgentRole.COPYEDIT, route="r", input_digest=str(index))
            database.update_task_status(database.create_task(task).id, status)

        total, rows = database.list_run_summaries(None, 2)
        assert total == 3
        assert [(run.id, counts) for run, counts in rows] == [
            (newest.id, {"pending": 2, "running": 1}),
            (middle.id, {}),
        ]
        assert oldest.id not in {run.id for run, _ in rows}


def test_summaries_filter_by_status_before_counting_and_limiting(tmp_path) -> None:
    with Database(tmp_path / "state.sqlite3") as database:
        _run(database, "2026-01-01T00:00:00+00:00")
        reviewing = [_run(database, f"2026-02-0{day}T00:00:00+00:00", RunStatus.REVIEWING) for day in (1, 2, 3)]

        total, rows = database.list_run_summaries(RunStatus.REVIEWING, 2)
        assert total == 3
        assert [run.id for run, _ in rows] == [reviewing[2].id, reviewing[1].id]
        assert database.list_run_summaries(RunStatus.COMPLETED, 5) == (0, [])
