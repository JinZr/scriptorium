import sqlite3

import pytest

from scriptorium.domain import (
    AgentRole,
    Artifact,
    Attempt,
    AttemptStatus,
    Event,
    Finding,
    FindingSeverity,
    FindingStatus,
    Patch,
    PatchStatus,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    Verification,
    VerificationResult,
)
from scriptorium.storage import _MIGRATION_1, ConflictError, Database


def make_run() -> Run:
    return Run(
        repository="/tmp/paper",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        profile="full",
        config_digest="c" * 64,
        frozen_config={"roles": {"copyedit": "primary"}},
        budget_usd=5.0,
    )


def create_completed_attempt(database: Database, run: Run) -> tuple[Task, Attempt]:
    task = database.create_task(
        Task(
            run_id=run.id,
            stage="review",
            role=AgentRole.COPYEDIT,
            route="primary",
            input_digest="d" * 64,
        )
    )
    attempt = database.create_attempt(
        Attempt(
            task_id=task.id,
            ordinal=1,
            runtime_name="codex",
            runtime_version="0.144.4",
            model="test-model",
            model_provider="openai",
        )
    )
    database.finish_attempt(
        attempt.id,
        AttemptStatus.COMPLETED,
        thread_id="thread-1",
        input_tokens=100,
        cached_input_tokens=20,
        output_tokens=30,
        reasoning_tokens=10,
        estimated_cost_usd=0.25,
        duration_ms=200,
    )
    return task, database.get_attempt(attempt.id)


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
        assert database.interrupt_running_attempts(run.id) == 1
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


def test_finding_decision_is_atomic_and_history_is_immutable(tmp_path) -> None:
    with Database(tmp_path / "state.sqlite3") as database:
        run = database.create_run(make_run())
        task, attempt = create_completed_attempt(database, run)
        finding = database.create_finding(
            Finding(
                run_id=run.id,
                task_id=task.id,
                attempt_id=attempt.id,
                fingerprint="f" * 64,
                role=AgentRole.COPYEDIT,
                category="language",
                severity=FindingSeverity.MAJOR,
                title="Ambiguous wording",
                claim="The sentence is ambiguous.",
                evidence=({"source_path": "main.tex", "start_line": 10, "end_line": 10},),
                explanation="Two readings are possible.",
                suggested_action="Rewrite the sentence.",
                confidence=0.9,
            )
        )

        decision = database.decide_finding(finding.id, "confirm", "The ambiguity is real.")

        assert database.get_finding(finding.id).status == FindingStatus.CONFIRMED
        assert database.list_decisions("finding", finding.id) == [decision]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.connection.execute(
                "UPDATE decisions SET reason = 'changed' WHERE id = ?",
                (decision.id,),
            )


def test_findings_deduplicate_only_by_exact_fingerprint(tmp_path) -> None:
    with Database(tmp_path / "state.sqlite3") as database:
        run = database.create_run(make_run())
        task, attempt = create_completed_attempt(database, run)

        def finding(fingerprint: str, title: str) -> Finding:
            return Finding(
                run_id=run.id,
                task_id=task.id,
                attempt_id=attempt.id,
                fingerprint=fingerprint,
                role=AgentRole.COPYEDIT,
                category="language",
                severity=FindingSeverity.MODERATE,
                title=title,
                claim="The sentence is ambiguous.",
                evidence=({"source_path": "main.tex", "start_line": 10, "end_line": 10},),
                explanation="Two readings are possible.",
                suggested_action="Rewrite the sentence.",
                confidence=0.9,
            )

        first = database.get_or_create_finding(finding("a" * 64, "Ambiguous wording"))
        duplicate = database.get_or_create_finding(finding("a" * 64, "Ambiguous wording"))
        similar = database.get_or_create_finding(finding("b" * 64, "Slightly ambiguous wording"))

        assert duplicate.id == first.id
        assert similar.id != first.id
        assert len(database.list_findings(run.id)) == 2


def test_patch_verification_and_artifact_metadata(tmp_path) -> None:
    with Database(tmp_path / "state.sqlite3") as database:
        run = database.create_run(make_run())
        _, attempt = create_completed_attempt(database, run)
        artifact = database.add_artifact(
            Artifact(
                digest="e" * 64,
                relative_path="sha256/ee/" + "e" * 62,
                size=123,
                media_type="text/x-diff",
            )
        )
        patch = database.create_patch(
            Patch(
                run_id=run.id,
                base_commit=run.commit_sha,
                diff_digest=artifact.digest,
                summary="Clarify one sentence.",
                edits=({"path": "main.tex", "before": "old", "after": "new"},),
                attempt_id=attempt.id,
            )
        )
        database.decide_patch(patch.id, "approve", "Reviewed locally.")
        verification = database.create_verification(
            Verification(
                patch_id=patch.id,
                result=VerificationResult.PASS,
                summary="No regression found.",
            )
        )

        assert database.get_artifact(artifact.digest) == artifact
        assert database.get_patch(patch.id).attempt_id == attempt.id
        assert database.get_patch(patch.id).status == PatchStatus.VERIFIED
        assert database.list_verifications(patch.id) == [verification]
        applied = database.update_patch(patch.id, PatchStatus.APPLIED)
        assert applied.applied_at is not None


def test_patch_records_the_generating_attempt(tmp_path) -> None:
    with Database(tmp_path / "state.sqlite3") as database:
        run = database.create_run(make_run())
        _, attempt = create_completed_attempt(database, run)
        artifact = database.add_artifact(
            Artifact(
                digest="f" * 64,
                relative_path="sha256/ff/" + "f" * 62,
                size=10,
                media_type="text/x-diff",
            )
        )

        patch = database.create_patch(
            Patch(
                run_id=run.id,
                base_commit=run.commit_sha,
                diff_digest=artifact.digest,
                summary="Generated by one durable attempt.",
                edits=(),
                attempt_id=attempt.id,
            )
        )

        assert database.get_patch(patch.id).attempt_id == attempt.id


def test_new_patch_requires_a_generating_attempt(tmp_path) -> None:
    with Database(tmp_path / "state.sqlite3") as database:
        with pytest.raises(ValueError, match="generating attempt_id"):
            database.create_patch(
                Patch(
                    run_id="run_missing",
                    base_commit="a" * 40,
                    diff_digest="d" * 64,
                    summary="Missing provenance",
                    edits=(),
                )
            )


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
