import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import subprocess
import sys
import threading

import pytest

from scriptorium.artifacts import ArtifactStore
from scriptorium.domain import Run
from scriptorium.errors import StateError
from scriptorium.service import ScriptoriumService
from scriptorium.storage import (
    _MIGRATION_1,
    _MIGRATION_2,
    _MIGRATION_3,
    _MIGRATION_4,
    _MIGRATION_5,
    ConflictError,
    Database,
)


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
            "external_tasks",
        }
        patch_columns = {row["name"] for row in database.connection.execute("PRAGMA table_info(patches)").fetchall()}
        assert "attempt_id" in patch_columns
        attempt_columns = {row["name"] for row in database.connection.execute("PRAGMA table_info(attempts)").fetchall()}
        assert "validation_report_artifact_digest" in attempt_columns
        assert {"external_client", "effort", "session_source"}.issubset(attempt_columns)
        finding_columns = {row["name"] for row in database.connection.execute("PRAGMA table_info(findings)").fetchall()}
        assert "consequence" in finding_columns
        run_columns = {row["name"] for row in database.connection.execute("PRAGMA table_info(runs)").fetchall()}
        assert "brief_digest" in run_columns
        assert database.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
        assert database.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert database.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert database.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_active_historical_database_remains_compatible_until_explicit_external_start(tmp_path) -> None:
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
        assert database.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 3
        with pytest.raises(ConflictError, match="must finish in its original version"):
            database.ensure_external_schema()
        assert database.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 3
        database.connection.execute("UPDATE runs SET status = 'completed' WHERE id = 'run_old'")
        database.ensure_external_schema()
        assert database.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
        assert database.get_patch("patch_old") == patch
        assert database.get_run("run_old").brief_digest is None


def test_concurrent_external_schema_upgrades_share_one_transaction(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for migration in (_MIGRATION_1, _MIGRATION_2, _MIGRATION_3):
        connection.executescript(migration)
    connection.close()

    with Database(path) as first, Database(path) as second:
        start = threading.Barrier(2)

        def upgrade(database):
            start.wait(timeout=5)
            database.ensure_external_schema()
            return database.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(upgrade, (first, second))) == [6, 6]
        for version in (4, 5, 6):
            assert (
                first.connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()[0]
                == 1
            )


def _insert_historical_finding(connection) -> None:
    connection.execute(
        """
        INSERT INTO runs (
            id, repository, commit_sha, tree_sha, profile, status, config_digest,
            frozen_config_json, budget_usd, estimated_cost_usd, created_at, updated_at, error
        ) VALUES ('run_old', '/tmp/paper', ?, ?, 'full', 'completed', ?, '{}', NULL, 0, ?, ?, NULL)
        """,
        ("a" * 40, "b" * 40, "c" * 64, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        """
        INSERT INTO tasks (id, run_id, stage, role, route, input_digest, status, created_at, updated_at)
        VALUES ('task_old', 'run_old', 'review', 'copyedit', '', ?, 'completed', ?, ?)
        """,
        ("d" * 64, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        """
        INSERT INTO attempts (id, task_id, ordinal, status, created_at)
        VALUES ('attempt_old', 'task_old', 1, 'completed', ?)
        """,
        ("2026-01-01T00:00:00+00:00",),
    )
    connection.execute(
        """
        INSERT INTO findings (
            id, run_id, task_id, attempt_id, fingerprint, role, category, severity, title, claim,
            evidence_json, explanation, suggested_action, confidence, status, created_at, updated_at
        ) VALUES (
            'finding_old', 'run_old', 'task_old', 'attempt_old', ?, 'copyedit', 'clarity', 'moderate', 'Title',
            'Claim', '[]', 'Explanation', 'Fix it.', 0.5, 'pending', ?, ?
        )
        """,
        ("f" * 64, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )


def test_external_database_adds_a_nullable_consequence_to_historical_findings(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for migration in (_MIGRATION_1, _MIGRATION_2, _MIGRATION_3, _MIGRATION_4):
        connection.executescript(migration)
    _insert_historical_finding(connection)
    connection.close()

    with Database(path) as database, Database(path) as reopened:
        finding = database.get_finding("finding_old")

        assert finding.consequence is None
        assert finding.title == "Title"
        assert reopened.get_finding("finding_old") == finding
        assert database.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
        assert (
            database.connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 5").fetchone()[0] == 1
        )


def test_historical_schema_three_findings_decode_without_a_consequence_column(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for migration in (_MIGRATION_1, _MIGRATION_2, _MIGRATION_3):
        connection.executescript(migration)
    _insert_historical_finding(connection)
    connection.close()

    with Database(path) as database:
        assert database.get_finding("finding_old").consequence is None
        assert database.get_run("run_old").brief_digest is None
        assert database.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 3


def test_version_five_database_adds_a_nullable_brief_digest_to_historical_runs(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for migration in (_MIGRATION_1, _MIGRATION_2, _MIGRATION_3, _MIGRATION_4, _MIGRATION_5):
        connection.executescript(migration)
    _insert_historical_finding(connection)
    connection.close()

    with Database(path) as database, Database(path) as reopened:
        run = database.get_run("run_old")

        assert run.brief_digest is None
        assert run.status.value == "completed"
        assert reopened.get_run("run_old") == run
        assert database.get_finding("finding_old").title == "Title"
        assert database.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
        assert (
            database.connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 6").fetchone()[0] == 1
        )


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


def test_version_two_database_adds_nullable_validation_report_pointer(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    connection.executescript(_MIGRATION_1)
    connection.executescript(_MIGRATION_2)
    connection.execute(
        """
        INSERT INTO runs (
            id, repository, commit_sha, tree_sha, profile, status, config_digest,
            frozen_config_json, budget_usd, estimated_cost_usd, created_at, updated_at, error
        ) VALUES ('run_old', '/tmp/paper', ?, ?, 'full', 'reviewing', ?, '{}', NULL, 0, ?, ?, NULL)
        """,
        ("a" * 40, "b" * 40, "c" * 64, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        """
        INSERT INTO tasks (
            id, run_id, stage, role, route, input_digest, status, created_at, updated_at
        ) VALUES ('task_old', 'run_old', 'review', 'copyedit', 'primary', ?, 'failed', ?, ?)
        """,
        ("d" * 64, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        """
        INSERT INTO attempts (
            id, task_id, ordinal, status, created_at, completed_at, error
        ) VALUES ('attempt_old', 'task_old', 1, 'failed', ?, ?, 'legacy validation failure')
        """,
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00"),
    )
    connection.close()

    with Database(path) as database:
        attempt = database.get_attempt("attempt_old")

        assert attempt.validation_report_artifact_digest is None
        assert attempt.error == "legacy validation failure"
        assert attempt.external_client is None
        assert database.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 3


def test_read_only_cli_commands_leave_historical_schema_three_unchanged(tmp_path) -> None:
    repo = tmp_path / "paper"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    path = repo / ".scriptorium" / "state.sqlite3"
    path.parent.mkdir()
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for migration in (_MIGRATION_1, _MIGRATION_2, _MIGRATION_3):
        connection.executescript(migration)
    connection.execute(
        """
        INSERT INTO runs (
            id, repository, commit_sha, tree_sha, profile, status, config_digest,
            frozen_config_json, budget_usd, estimated_cost_usd, created_at, updated_at, error
        ) VALUES ('run_old', ?, ?, ?, 'full', 'reviewing', ?, ?, NULL, 0, ?, ?, NULL)
        """,
        (
            str(repo),
            "a" * 40,
            "b" * 40,
            "c" * 64,
            json.dumps({"profile_roles": ["copyedit"]}),
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    connection.execute(
        """
        INSERT INTO tasks (id, run_id, stage, role, route, input_digest, status, created_at, updated_at)
        VALUES ('task_old', 'run_old', 'review', 'copyedit', 'primary', ?, 'running', ?, ?)
        """,
        ("d" * 64, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        """
        INSERT INTO attempts (id, task_id, ordinal, status, created_at)
        VALUES ('attempt_old', 'task_old', 1, 'running', ?)
        """,
        ("2026-01-01T00:00:00+00:00",),
    )
    connection.close()

    for command, expected_code in (
        (["run", "status", "run_old"], 0),
        (["run", "report", "run_old", "--format", "json"], 0),
        (["run", "gate", "run_old"], 1),
    ):
        result = subprocess.run(
            [sys.executable, "-m", "scriptorium", "--json", *command],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode == expected_code, result.stderr or result.stdout
        assert json.loads(result.stdout)["data"]
        with sqlite3.connect(path) as check:
            assert check.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 3

    with ScriptoriumService(repo) as service:
        with pytest.raises(StateError, match="must finish in its original version"):
            asyncio.run(service.start_run("HEAD", "full"))
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 3
