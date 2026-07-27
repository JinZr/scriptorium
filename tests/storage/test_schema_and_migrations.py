import sqlite3

from scriptorium.storage import _MIGRATION_1, Database


def test_schema_and_pragmas(tmp_path) -> None:
    with Database(tmp_path / "state.sqlite3") as database:
        table_names = {
            row["name"]
            for row in database.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }

        assert table_names == {
            "schema_migrations",
            "runs",
            "tasks",
            "attempts",
            "artifacts",
            "findings",
            "decisions",
            "patches",
            "verifications",
            "events",
        }
        patch_columns = {row["name"] for row in database.connection.execute("PRAGMA table_info(patches)").fetchall()}
        assert "attempt_id" in patch_columns
        assert database.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
        assert database.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert database.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert database.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_existing_database_is_upgraded_without_rewriting_old_patch(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    connection.executescript(_MIGRATION_1)
    connection.execute(
        """
        INSERT INTO runs (
            id, repository, commit_sha, tree_sha, profile, status, config_digest,
            frozen_config_json, budget_usd, estimated_cost_usd, created_at, updated_at, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "run_old",
            "/tmp/paper",
            "a" * 40,
            "b" * 40,
            "full",
            "awaiting_patch_approval",
            "c" * 64,
            "{}",
            None,
            0,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            None,
        ),
    )
    connection.execute(
        """
        INSERT INTO artifacts (digest, relative_path, size, media_type, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "d" * 64,
            "sha256/dd/" + "d" * 62,
            1,
            "text/x-diff",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    connection.execute(
        """
        INSERT INTO patches (
            id, run_id, base_commit, diff_digest, summary, edits_json, status,
            build_succeeded, created_at, updated_at, applied_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "patch_old",
            "run_old",
            "a" * 40,
            "d" * 64,
            "Old patch",
            "[]",
            "proposed",
            1,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            None,
        ),
    )
    connection.close()

    with Database(path) as database:
        patch = database.get_patch("patch_old")

        assert patch.attempt_id is None
        assert database.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
