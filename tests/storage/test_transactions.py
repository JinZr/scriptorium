import sqlite3

import pytest

from scriptorium.domain import Event, RunStatus
from scriptorium.storage import Database

from ._factories import make_run


def test_transaction_rolls_back_state_and_event_together(tmp_path) -> None:
    with Database(tmp_path / "state.sqlite3") as database:
        run = database.create_run(make_run())
        duplicate_event_id = "event_duplicate"
        database.append_event(
            Event(
                id=duplicate_event_id,
                run_id=run.id,
                event_type="test",
                entity_type="run",
                entity_id=run.id,
            )
        )

        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE runs SET status = ? WHERE id = ?",
                    (RunStatus.FAILED.value, run.id),
                )
                connection.execute(
                    """
                    INSERT INTO events (
                        id, run_id, event_type, entity_type, entity_id, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (duplicate_event_id, run.id, "duplicate", "run", run.id, "{}", "now"),
                )

        assert database.get_run(run.id).status == RunStatus.PREPARING
