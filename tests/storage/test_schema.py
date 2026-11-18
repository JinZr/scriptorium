from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from scriptorium.artifacts import ArtifactStore
from scriptorium.domain import Run
from scriptorium.storage import Database

from ._factories import create_completed_attempt, make_run


def _schema(connection) -> list[tuple]:
    return [tuple(row) for row in connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name")]


def test_schema_and_pragmas(tmp_path) -> None:
    with Database(tmp_path / "state.sqlite3") as database:
        table_names = {
            row["name"]
            for row in database.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }

        assert table_names == {
            "runs",
            "tasks",
            "attempts",
            "artifacts",
            "findings",
            "decisions",
            "patches",
            "verifications",
            "events",
            "external_tasks",
        }
        patch_columns = {row["name"] for row in database.connection.execute("PRAGMA table_info(patches)").fetchall()}
        assert "attempt_id" in patch_columns
        attempt_columns = {row["name"] for row in database.connection.execute("PRAGMA table_info(attempts)").fetchall()}
        assert "validation_report_artifact_digest" in attempt_columns
        assert {"external_client", "effort", "session_source"}.issubset(attempt_columns)
        finding_columns = {row["name"] for row in database.connection.execute("PRAGMA table_info(findings)").fetchall()}
        assert {"consequence", "affected_claim"} <= finding_columns
        run_columns = {row["name"] for row in database.connection.execute("PRAGMA table_info(runs)").fetchall()}
        assert "brief_digest" in run_columns
        external_columns = {
            row["name"] for row in database.connection.execute("PRAGMA table_info(external_tasks)").fetchall()
        }
        assert "example_digest" in external_columns
        assert database.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert database.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert database.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_reopening_a_database_keeps_its_schema_and_records(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    run = make_run()
    with Database(path) as database:
        database.create_run(run)
        task, attempt = create_completed_attempt(database, run)
        stored = database.get_run(run.id)
        schema = _schema(database.connection)

    with Database(path) as reopened:
        assert _schema(reopened.connection) == schema
        assert reopened.get_run(run.id) == stored
        assert reopened.get_attempt(attempt.id).task_id == task.id
        with pytest.raises(sqlite3.IntegrityError, match="terminal attempts are immutable"):
            reopened.connection.execute("UPDATE attempts SET error = 'changed' WHERE id = ?", (attempt.id,))


def test_an_unknown_leftover_table_is_ignored(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    Database(path).close()
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    connection.commit()
    connection.close()

    with Database(path) as database:
        database.create_run(make_run())
        assert database.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0


def test_concurrent_first_opens_of_a_new_database_share_one_schema(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    start = threading.Barrier(4)

    def open_database(_):
        start.wait(timeout=5)
        with Database(path) as database:
            return _schema(database.connection)

    with ThreadPoolExecutor(max_workers=4) as pool:
        schemas = list(pool.map(open_database, range(4)))

    assert all(schema == schemas[0] for schema in schemas)
    assert len([item for item in schemas[0] if item[0] == "trigger"]) == 8


def test_a_run_records_its_brief_artifact_digest(tmp_path) -> None:
    with Database(tmp_path / "state.sqlite3") as database:
        artifact = ArtifactStore(tmp_path / "artifacts").put_text("{}", "application/json")
        database.record_artifact(artifact)
        run = Run("/tmp/paper", "a" * 40, "b" * 40, "full", "c" * 64, {}, brief_digest=artifact.digest)
        database.create_run(run)
        plain = Run("/tmp/paper", "a" * 40, "b" * 40, "full", "c" * 64, {})
        database.create_run(plain)

        assert database.get_run(run.id).brief_digest == artifact.digest
        assert database.get_run(plain.id).brief_digest is None
        with pytest.raises(sqlite3.IntegrityError):
            database.create_run(Run("/tmp/paper", "a" * 40, "b" * 40, "full", "c" * 64, {}, brief_digest="e" * 64))
