from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator, Mapping, Sequence

from scriptorium.domain import (
    AgentRole,
    Artifact,
    Attempt,
    AttemptStatus,
    Decision,
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
    canonical_json,
    utc_now,
    validate_run_transition,
    validate_task_transition,
)

SCHEMA_VERSION = 2


_MIGRATION_1 = """
BEGIN IMMEDIATE;

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    tree_sha TEXT NOT NULL,
    profile TEXT NOT NULL,
    status TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    frozen_config_json TEXT NOT NULL,
    budget_usd REAL,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    stage TEXT NOT NULL,
    role TEXT NOT NULL,
    route TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, stage, role, route, input_digest)
);

CREATE TABLE artifacts (
    digest TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE attempts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    thread_id TEXT,
    runtime_name TEXT,
    runtime_version TEXT,
    model TEXT,
    model_provider TEXT,
    prompt_digest TEXT,
    schema_digest TEXT,
    bundle_digest TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    trace_artifact_digest TEXT REFERENCES artifacts(digest) ON DELETE RESTRICT,
    output_artifact_digest TEXT REFERENCES artifacts(digest) ON DELETE RESTRICT,
    duration_ms INTEGER,
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (task_id, ordinal)
);

CREATE TABLE findings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
    fingerprint TEXT NOT NULL,
    role TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    claim TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    explanation TEXT NOT NULL,
    suggested_action TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, fingerprint)
);

CREATE TABLE decisions (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE patches (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    base_commit TEXT NOT NULL,
    diff_digest TEXT NOT NULL REFERENCES artifacts(digest) ON DELETE RESTRICT,
    summary TEXT NOT NULL,
    edits_json TEXT NOT NULL,
    status TEXT NOT NULL,
    build_succeeded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    applied_at TEXT,
    UNIQUE (run_id, base_commit, diff_digest)
);

CREATE TABLE verifications (
    id TEXT PRIMARY KEY,
    patch_id TEXT NOT NULL REFERENCES patches(id) ON DELETE RESTRICT,
    attempt_id TEXT REFERENCES attempts(id) ON DELETE RESTRICT,
    result TEXT NOT NULL,
    summary TEXT NOT NULL,
    artifact_digest TEXT REFERENCES artifacts(digest) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER artifacts_no_update
BEFORE UPDATE ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifacts are immutable');
END;

CREATE TRIGGER artifacts_no_delete
BEFORE DELETE ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifacts are immutable');
END;

CREATE TRIGGER attempts_no_delete
BEFORE DELETE ON attempts
BEGIN
    SELECT RAISE(ABORT, 'attempts are append-only');
END;

CREATE TRIGGER attempts_no_terminal_update
BEFORE UPDATE ON attempts
WHEN OLD.status <> 'running'
BEGIN
    SELECT RAISE(ABORT, 'terminal attempts are immutable');
END;

CREATE TRIGGER decisions_no_update
BEFORE UPDATE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions are append-only');
END;

CREATE TRIGGER decisions_no_delete
BEFORE DELETE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions are append-only');
END;

CREATE TRIGGER events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

INSERT INTO schema_migrations (version, applied_at) VALUES (1, CURRENT_TIMESTAMP);
COMMIT;
"""


_MIGRATION_2 = """
BEGIN IMMEDIATE;
ALTER TABLE patches ADD COLUMN attempt_id TEXT REFERENCES attempts(id) ON DELETE RESTRICT;
INSERT INTO schema_migrations (version, applied_at) VALUES (2, CURRENT_TIMESTAMP);
COMMIT;
"""


class StorageError(RuntimeError):
    pass


class NotFoundError(StorageError):
    pass


class ConflictError(StorageError):
    pass


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = self.connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            version = int(row["version"])
            if version > SCHEMA_VERSION:
                raise StorageError(
                    f"database schema version {version} is newer than supported version {SCHEMA_VERSION}"
                )
            if version == 0:
                self.connection.executescript(_MIGRATION_1)
                version = 1
            if version == 1:
                self.connection.executescript(_MIGRATION_2)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def create_run(self, run: Run) -> Run:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    id, repository, commit_sha, tree_sha, profile, status, config_digest, frozen_config_json,
                    budget_usd, estimated_cost_usd, created_at, updated_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.repository,
                    run.commit_sha,
                    run.tree_sha,
                    run.profile,
                    run.status.value,
                    run.config_digest,
                    canonical_json(run.frozen_config),
                    run.budget_usd,
                    run.estimated_cost_usd,
                    run.created_at,
                    run.updated_at,
                    run.error,
                ),
            )
            self._append_event_row(
                connection,
                Event(
                    run_id=run.id,
                    event_type="run.created",
                    entity_type="run",
                    entity_id=run.id,
                    payload={"status": run.status.value},
                ),
            )
        return run

    def get_run(self, run_id: str) -> Run:
        row = self.connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"run not found: {run_id}")
        return self._run_from_row(row)

    def list_runs(self) -> list[Run]:
        rows = self.connection.execute("SELECT * FROM runs ORDER BY created_at, id").fetchall()
        return [self._run_from_row(row) for row in rows]

    def update_run_status(self, run_id: str, status: RunStatus, error: str | None = None) -> Run:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"run not found: {run_id}")
            current = RunStatus(row["status"])
            validate_run_transition(current, status)
            if current == status and row["error"] == error:
                return self._run_from_row(row)
            updated_at = utc_now()
            connection.execute(
                "UPDATE runs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status.value, error, updated_at, run_id),
            )
            self._append_event_row(
                connection,
                Event(
                    run_id=run_id,
                    event_type="run.status_changed",
                    entity_type="run",
                    entity_id=run_id,
                    payload={"from": current.value, "to": status.value, "error": error},
                ),
            )
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_from_row(row)

    def update_run(self, run_id: str, status: RunStatus, error: str | None = None) -> Run:
        return self.update_run_status(run_id, status, error)

    def add_run_cost(self, run_id: str, amount_usd: float) -> Run:
        if amount_usd < 0:
            raise ValueError("amount_usd must be non-negative")
        with self.transaction() as connection:
            result = connection.execute(
                "UPDATE runs SET estimated_cost_usd = estimated_cost_usd + ?, updated_at = ? WHERE id = ?",
                (amount_usd, utc_now(), run_id),
            )
            if result.rowcount != 1:
                raise NotFoundError(f"run not found: {run_id}")
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_from_row(row)

    def create_task(self, task: Task) -> Task:
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO tasks (
                        id, run_id, stage, role, route, input_digest, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.id,
                        task.run_id,
                        task.stage,
                        task.role.value,
                        task.route,
                        task.input_digest,
                        task.status.value,
                        task.created_at,
                        task.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("task already exists for run, stage, role, route, and input digest") from exc
            self._append_event_row(
                connection,
                Event(
                    run_id=task.run_id,
                    event_type="task.created",
                    entity_type="task",
                    entity_id=task.id,
                    payload={"stage": task.stage, "role": task.role.value, "route": task.route},
                ),
            )
        return task

    def get_task(self, task_id: str) -> Task:
        row = self.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"task not found: {task_id}")
        return self._task_from_row(row)

    def find_task(
        self,
        run_id: str,
        stage: str,
        role: AgentRole,
        route: str,
        input_digest: str,
    ) -> Task | None:
        row = self.connection.execute(
            """
            SELECT * FROM tasks
            WHERE run_id = ? AND stage = ? AND role = ? AND route = ? AND input_digest = ?
            """,
            (run_id, stage, role.value, route, input_digest),
        ).fetchone()
        return self._task_from_row(row) if row is not None else None

    def list_tasks(self, run_id: str) -> list[Task]:
        rows = self.connection.execute(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at, id",
            (run_id,),
        ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def update_task_status(self, task_id: str, status: TaskStatus) -> Task:
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT tasks.*, runs.id AS event_run_id
                FROM tasks JOIN runs ON runs.id = tasks.run_id
                WHERE tasks.id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"task not found: {task_id}")
            current = TaskStatus(row["status"])
            validate_task_transition(current, status)
            if current == status:
                return self._task_from_row(row)
            updated_at = utc_now()
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, updated_at, task_id),
            )
            self._append_event_row(
                connection,
                Event(
                    run_id=row["event_run_id"],
                    event_type="task.status_changed",
                    entity_type="task",
                    entity_id=task_id,
                    payload={"from": current.value, "to": status.value},
                ),
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_from_row(row)

    def create_attempt(self, attempt: Attempt) -> Attempt:
        if attempt.status != AttemptStatus.RUNNING:
            raise ValueError("new attempts must have running status")
        with self.transaction() as connection:
            task_row = connection.execute("SELECT * FROM tasks WHERE id = ?", (attempt.task_id,)).fetchone()
            if task_row is None:
                raise NotFoundError(f"task not found: {attempt.task_id}")
            task_status = TaskStatus(task_row["status"])
            if task_status == TaskStatus.RUNNING:
                raise ConflictError(f"task already has a running attempt: {attempt.task_id}")
            validate_task_transition(task_status, TaskStatus.RUNNING)
            expected_ordinal = connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS ordinal FROM attempts WHERE task_id = ?",
                (attempt.task_id,),
            ).fetchone()["ordinal"]
            if attempt.ordinal != expected_ordinal:
                raise ConflictError(
                    f"attempt ordinal must be {expected_ordinal} for task {attempt.task_id}, got {attempt.ordinal}"
                )
            connection.execute(
                """
                INSERT INTO attempts (
                    id, task_id, ordinal, status, thread_id, runtime_name, runtime_version, model,
                    model_provider, prompt_digest, schema_digest, bundle_digest, input_tokens,
                    cached_input_tokens, output_tokens, reasoning_tokens, estimated_cost_usd,
                    trace_artifact_digest, output_artifact_digest, duration_ms, error, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._attempt_values(attempt),
            )
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.RUNNING.value, utc_now(), attempt.task_id),
            )
            self._append_event_row(
                connection,
                Event(
                    run_id=task_row["run_id"],
                    event_type="attempt.started",
                    entity_type="attempt",
                    entity_id=attempt.id,
                    payload={"task_id": attempt.task_id, "ordinal": attempt.ordinal},
                ),
            )
        return attempt

    def begin_attempt(self, task_id: str, **values: Any) -> Attempt:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(ordinal), 0) + 1 AS ordinal FROM attempts WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        attempt = Attempt(task_id=task_id, ordinal=int(row["ordinal"]), **values)
        return self.create_attempt(attempt)

    def get_attempt(self, attempt_id: str) -> Attempt:
        row = self.connection.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"attempt not found: {attempt_id}")
        return self._attempt_from_row(row)

    def list_attempts(self, task_id: str) -> list[Attempt]:
        rows = self.connection.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY ordinal",
            (task_id,),
        ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    def finish_attempt(
        self,
        attempt_id: str,
        status: AttemptStatus,
        *,
        thread_id: str | None = None,
        runtime_name: str | None = None,
        runtime_version: str | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
        trace_artifact_digest: str | None = None,
        output_artifact_digest: str | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> Attempt:
        if status == AttemptStatus.RUNNING:
            raise ValueError("finish status must be terminal")
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT attempts.*, tasks.run_id
                FROM attempts JOIN tasks ON tasks.id = attempts.task_id
                WHERE attempts.id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"attempt not found: {attempt_id}")
            if AttemptStatus(row["status"]) != AttemptStatus.RUNNING:
                raise ConflictError(f"attempt is already terminal: {attempt_id}")
            completed_at = utc_now()
            connection.execute(
                """
                UPDATE attempts SET
                    status = ?, thread_id = COALESCE(?, thread_id),
                    runtime_name = COALESCE(?, runtime_name),
                    runtime_version = COALESCE(?, runtime_version),
                    model = COALESCE(?, model),
                    model_provider = COALESCE(?, model_provider),
                    input_tokens = ?, cached_input_tokens = ?, output_tokens = ?,
                    reasoning_tokens = ?, estimated_cost_usd = ?, trace_artifact_digest = ?,
                    output_artifact_digest = ?, duration_ms = ?, error = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    thread_id,
                    runtime_name,
                    runtime_version,
                    model,
                    model_provider,
                    input_tokens,
                    cached_input_tokens,
                    output_tokens,
                    reasoning_tokens,
                    estimated_cost_usd,
                    trace_artifact_digest,
                    output_artifact_digest,
                    duration_ms,
                    error,
                    completed_at,
                    attempt_id,
                ),
            )
            task_status = TaskStatus(status.value)
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (task_status.value, completed_at, row["task_id"]),
            )
            connection.execute(
                "UPDATE runs SET estimated_cost_usd = estimated_cost_usd + ?, updated_at = ? WHERE id = ?",
                (estimated_cost_usd, completed_at, row["run_id"]),
            )
            self._append_event_row(
                connection,
                Event(
                    run_id=row["run_id"],
                    event_type="attempt.finished",
                    entity_type="attempt",
                    entity_id=attempt_id,
                    payload={"status": status.value, "task_id": row["task_id"], "ordinal": row["ordinal"]},
                ),
            )
            row = connection.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        return self._attempt_from_row(row)

    def update_attempt(
        self,
        attempt_id: str,
        status: AttemptStatus,
        **result: Any,
    ) -> Attempt:
        return self.finish_attempt(attempt_id, status, **result)

    def interrupt_running_attempts(self, run_id: str) -> int:
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT attempts.id, attempts.task_id, attempts.ordinal
                FROM attempts
                JOIN tasks ON tasks.id = attempts.task_id
                WHERE tasks.run_id = ? AND attempts.status = ?
                ORDER BY attempts.created_at, attempts.id
                """,
                (run_id, AttemptStatus.RUNNING.value),
            ).fetchall()
            completed_at = utc_now()
            for row in rows:
                connection.execute(
                    "UPDATE attempts SET status = ?, completed_at = ?, error = ? WHERE id = ?",
                    (
                        AttemptStatus.INTERRUPTED.value,
                        completed_at,
                        "process exited before attempt completion",
                        row["id"],
                    ),
                )
                connection.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (TaskStatus.INTERRUPTED.value, completed_at, row["task_id"]),
                )
                self._append_event_row(
                    connection,
                    Event(
                        run_id=run_id,
                        event_type="attempt.interrupted",
                        entity_type="attempt",
                        entity_id=row["id"],
                        payload={"task_id": row["task_id"], "ordinal": row["ordinal"]},
                    ),
                )
        return len(rows)

    def cancel_incomplete_tasks(self, run_id: str) -> int:
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE run_id = ? AND status IN (?, ?, ?)
                ORDER BY created_at, id
                """,
                (
                    run_id,
                    TaskStatus.PENDING.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.INTERRUPTED.value,
                ),
            ).fetchall()
            updated_at = utc_now()
            for row in rows:
                current = TaskStatus(row["status"])
                validate_task_transition(current, TaskStatus.CANCELLED)
                connection.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (TaskStatus.CANCELLED.value, updated_at, row["id"]),
                )
                self._append_event_row(
                    connection,
                    Event(
                        run_id=run_id,
                        event_type="task.status_changed",
                        entity_type="task",
                        entity_id=row["id"],
                        payload={"from": current.value, "to": TaskStatus.CANCELLED.value},
                    ),
                )
        return len(rows)

    def record_artifact(self, artifact: Artifact) -> Artifact:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO artifacts (digest, relative_path, size, media_type, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    artifact.digest,
                    artifact.relative_path,
                    artifact.size,
                    artifact.media_type,
                    artifact.created_at,
                ),
            )
            row = connection.execute("SELECT * FROM artifacts WHERE digest = ?", (artifact.digest,)).fetchone()
            existing = self._artifact_from_row(row)
            if existing.relative_path != artifact.relative_path or existing.size != artifact.size:
                raise ConflictError(f"artifact metadata conflicts for digest {artifact.digest}")
        return existing

    def add_artifact(self, artifact: Artifact) -> Artifact:
        return self.record_artifact(artifact)

    def get_artifact(self, digest: str) -> Artifact:
        row = self.connection.execute("SELECT * FROM artifacts WHERE digest = ?", (digest,)).fetchone()
        if row is None:
            raise NotFoundError(f"artifact not found: {digest}")
        return self._artifact_from_row(row)

    def create_finding(self, finding: Finding) -> Finding:
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO findings (
                        id, run_id, task_id, attempt_id, fingerprint, role, category, severity, title,
                        claim, evidence_json, explanation, suggested_action, confidence, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        finding.id,
                        finding.run_id,
                        finding.task_id,
                        finding.attempt_id,
                        finding.fingerprint,
                        finding.role.value,
                        finding.category,
                        finding.severity.value,
                        finding.title,
                        finding.claim,
                        canonical_json(finding.evidence),
                        finding.explanation,
                        finding.suggested_action,
                        finding.confidence,
                        finding.status.value,
                        finding.created_at,
                        finding.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"finding already exists for fingerprint {finding.fingerprint}") from exc
            self._append_event_row(
                connection,
                Event(
                    run_id=finding.run_id,
                    event_type="finding.created",
                    entity_type="finding",
                    entity_id=finding.id,
                    payload={"severity": finding.severity.value, "role": finding.role.value},
                ),
            )
        return finding

    def get_or_create_finding(self, finding: Finding) -> Finding:
        row = self.connection.execute(
            "SELECT * FROM findings WHERE run_id = ? AND fingerprint = ?",
            (finding.run_id, finding.fingerprint),
        ).fetchone()
        if row is not None:
            return self._finding_from_row(row)
        try:
            return self.create_finding(finding)
        except ConflictError:
            row = self.connection.execute(
                "SELECT * FROM findings WHERE run_id = ? AND fingerprint = ?",
                (finding.run_id, finding.fingerprint),
            ).fetchone()
            return self._finding_from_row(row)

    def get_finding(self, finding_id: str) -> Finding:
        row = self.connection.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"finding not found: {finding_id}")
        return self._finding_from_row(row)

    def list_findings(
        self,
        run_id: str,
        statuses: Sequence[FindingStatus] | None = None,
    ) -> list[Finding]:
        parameters: list[Any] = [run_id]
        query = "SELECT * FROM findings WHERE run_id = ?"
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            parameters.extend(status.value for status in statuses)
        query += """
            ORDER BY CASE severity
                WHEN 'blocker' THEN 0
                WHEN 'major' THEN 1
                WHEN 'moderate' THEN 2
                WHEN 'minor' THEN 3
                ELSE 4
            END, created_at, id
        """
        rows = self.connection.execute(query, parameters).fetchall()
        return [self._finding_from_row(row) for row in rows]

    def decide_finding(
        self,
        finding_id: str,
        decision: str,
        reason: str,
        actor: str = "user",
    ) -> Decision:
        status_by_decision = {
            "confirm": FindingStatus.CONFIRMED,
            "reject": FindingStatus.REJECTED,
            "waive": FindingStatus.WAIVED,
        }
        if decision not in status_by_decision:
            raise ValueError(f"invalid finding decision: {decision}")
        if not reason.strip():
            raise ValueError("decision reason is required")
        record = Decision(
            target_type="finding",
            target_id=finding_id,
            decision=decision,
            reason=reason,
            actor=actor,
        )
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"finding not found: {finding_id}")
            connection.execute(
                "UPDATE findings SET status = ?, updated_at = ? WHERE id = ?",
                (status_by_decision[decision].value, record.created_at, finding_id),
            )
            self._append_decision_row(connection, record)
            self._append_event_row(
                connection,
                Event(
                    run_id=row["run_id"],
                    event_type="finding.decided",
                    entity_type="finding",
                    entity_id=finding_id,
                    payload={"decision": decision, "reason": reason, "actor": actor},
                ),
            )
        return record

    def list_decisions(self, target_type: str, target_id: str) -> list[Decision]:
        rows = self.connection.execute(
            """
            SELECT * FROM decisions
            WHERE target_type = ? AND target_id = ?
            ORDER BY created_at, id
            """,
            (target_type, target_id),
        ).fetchall()
        return [self._decision_from_row(row) for row in rows]

    def append_decision(self, decision: Decision) -> Decision:
        if not decision.reason.strip():
            raise ValueError("decision reason is required")
        if decision.target_type == "finding":
            status_by_decision = {
                "confirm": FindingStatus.CONFIRMED,
                "reject": FindingStatus.REJECTED,
                "waive": FindingStatus.WAIVED,
            }
            try:
                status = status_by_decision[decision.decision]
            except KeyError as exc:
                raise ValueError(f"invalid finding decision: {decision.decision}") from exc
            with self.transaction() as connection:
                row = connection.execute(
                    "SELECT run_id FROM findings WHERE id = ?",
                    (decision.target_id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"finding not found: {decision.target_id}")
                connection.execute(
                    "UPDATE findings SET status = ?, updated_at = ? WHERE id = ?",
                    (status.value, decision.created_at, decision.target_id),
                )
                self._append_decision_row(connection, decision)
                self._append_event_row(
                    connection,
                    Event(
                        run_id=row["run_id"],
                        event_type="finding.decided",
                        entity_type="finding",
                        entity_id=decision.target_id,
                        payload={
                            "decision": decision.decision,
                            "reason": decision.reason,
                            "actor": decision.actor,
                        },
                    ),
                )
            return decision
        if decision.target_type == "patch":
            status_by_decision = {"approve": PatchStatus.APPROVED, "reject": PatchStatus.REJECTED}
            try:
                status = status_by_decision[decision.decision]
            except KeyError as exc:
                raise ValueError(f"invalid patch decision: {decision.decision}") from exc
            with self.transaction() as connection:
                row = connection.execute(
                    "SELECT run_id, status FROM patches WHERE id = ?",
                    (decision.target_id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"patch not found: {decision.target_id}")
                if PatchStatus(row["status"]) not in {
                    PatchStatus.PROPOSED,
                    PatchStatus.APPROVED,
                    PatchStatus.REJECTED,
                }:
                    raise ConflictError(f"patch cannot be decided in status {row['status']}")
                connection.execute(
                    "UPDATE patches SET status = ?, updated_at = ? WHERE id = ?",
                    (status.value, decision.created_at, decision.target_id),
                )
                self._append_decision_row(connection, decision)
                self._append_event_row(
                    connection,
                    Event(
                        run_id=row["run_id"],
                        event_type="patch.decided",
                        entity_type="patch",
                        entity_id=decision.target_id,
                        payload={
                            "decision": decision.decision,
                            "reason": decision.reason,
                            "actor": decision.actor,
                        },
                    ),
                )
            return decision
        with self.transaction() as connection:
            self._append_decision_row(connection, decision)
        return decision

    def create_patch(self, patch: Patch) -> Patch:
        if patch.attempt_id is None:
            raise ValueError("new patches require a generating attempt_id")
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO patches (
                        id, run_id, base_commit, diff_digest, summary, edits_json, attempt_id,
                        status, build_succeeded, created_at, updated_at, applied_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        patch.id,
                        patch.run_id,
                        patch.base_commit,
                        patch.diff_digest,
                        patch.summary,
                        canonical_json(patch.edits),
                        patch.attempt_id,
                        patch.status.value,
                        int(patch.build_succeeded),
                        patch.created_at,
                        patch.updated_at,
                        patch.applied_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("patch already exists for run, base commit, and diff digest") from exc
            self._append_event_row(
                connection,
                Event(
                    run_id=patch.run_id,
                    event_type="patch.created",
                    entity_type="patch",
                    entity_id=patch.id,
                    payload={
                        "status": patch.status.value,
                        "diff_digest": patch.diff_digest,
                        "attempt_id": patch.attempt_id,
                    },
                ),
            )
        return patch

    def get_patch(self, patch_id: str) -> Patch:
        row = self.connection.execute("SELECT * FROM patches WHERE id = ?", (patch_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"patch not found: {patch_id}")
        return self._patch_from_row(row)

    def list_patches(self, run_id: str) -> list[Patch]:
        rows = self.connection.execute(
            "SELECT * FROM patches WHERE run_id = ? ORDER BY updated_at, id",
            (run_id,),
        ).fetchall()
        return [self._patch_from_row(row) for row in rows]

    def repropose_patch(
        self,
        patch_id: str,
        *,
        attempt_id: str,
        summary: str,
        edits: Sequence[Mapping[str, Any]],
        build_succeeded: bool,
    ) -> Patch:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM patches WHERE id = ?", (patch_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"patch not found: {patch_id}")
            current = PatchStatus(row["status"])
            if current != PatchStatus.REJECTED:
                raise ConflictError(f"patch cannot be reproposed in status {current.value}")
            updated_at = utc_now()
            connection.execute(
                """
                UPDATE patches
                SET summary = ?, edits_json = ?, attempt_id = ?, status = ?,
                    build_succeeded = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    summary,
                    canonical_json(edits),
                    attempt_id,
                    PatchStatus.PROPOSED.value,
                    int(build_succeeded),
                    updated_at,
                    patch_id,
                ),
            )
            self._append_event_row(
                connection,
                Event(
                    run_id=row["run_id"],
                    event_type="patch.reproposed",
                    entity_type="patch",
                    entity_id=patch_id,
                    payload={
                        "from": current.value,
                        "to": PatchStatus.PROPOSED.value,
                        "previous_attempt_id": row["attempt_id"],
                        "attempt_id": attempt_id,
                        "diff_digest": row["diff_digest"],
                    },
                ),
            )
            row = connection.execute("SELECT * FROM patches WHERE id = ?", (patch_id,)).fetchone()
        return self._patch_from_row(row)

    def decide_patch(self, patch_id: str, decision: str, reason: str, actor: str = "user") -> Decision:
        status_by_decision = {"approve": PatchStatus.APPROVED, "reject": PatchStatus.REJECTED}
        if decision not in status_by_decision:
            raise ValueError(f"invalid patch decision: {decision}")
        if not reason.strip():
            raise ValueError("decision reason is required")
        record = Decision(
            target_type="patch",
            target_id=patch_id,
            decision=decision,
            reason=reason,
            actor=actor,
        )
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM patches WHERE id = ?", (patch_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"patch not found: {patch_id}")
            current = PatchStatus(row["status"])
            if current not in {PatchStatus.PROPOSED, PatchStatus.APPROVED, PatchStatus.REJECTED}:
                raise ConflictError(f"patch cannot be decided in status {current.value}")
            connection.execute(
                "UPDATE patches SET status = ?, updated_at = ? WHERE id = ?",
                (status_by_decision[decision].value, record.created_at, patch_id),
            )
            self._append_decision_row(connection, record)
            self._append_event_row(
                connection,
                Event(
                    run_id=row["run_id"],
                    event_type="patch.decided",
                    entity_type="patch",
                    entity_id=patch_id,
                    payload={"decision": decision, "reason": reason, "actor": actor},
                ),
            )
        return record

    def update_patch_status(
        self,
        patch_id: str,
        status: PatchStatus,
        *,
        build_succeeded: bool | None = None,
    ) -> Patch:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM patches WHERE id = ?", (patch_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"patch not found: {patch_id}")
            current = PatchStatus(row["status"])
            allowed = {
                PatchStatus.PROPOSED: {PatchStatus.APPROVED, PatchStatus.REJECTED},
                PatchStatus.APPROVED: {PatchStatus.REJECTED, PatchStatus.VERIFIED, PatchStatus.STALE},
                PatchStatus.REJECTED: set(),
                PatchStatus.VERIFIED: {PatchStatus.APPLIED, PatchStatus.STALE},
                PatchStatus.APPLIED: set(),
                PatchStatus.STALE: set(),
            }
            if current != status and status not in allowed[current]:
                raise ValueError(f"invalid patch status transition: {current.value} -> {status.value}")
            updated_at = utc_now()
            applied_at = updated_at if status == PatchStatus.APPLIED else row["applied_at"]
            build_value = row["build_succeeded"] if build_succeeded is None else int(build_succeeded)
            connection.execute(
                """
                UPDATE patches
                SET status = ?, build_succeeded = ?, updated_at = ?, applied_at = ?
                WHERE id = ?
                """,
                (status.value, build_value, updated_at, applied_at, patch_id),
            )
            self._append_event_row(
                connection,
                Event(
                    run_id=row["run_id"],
                    event_type="patch.status_changed",
                    entity_type="patch",
                    entity_id=patch_id,
                    payload={"from": current.value, "to": status.value},
                ),
            )
            row = connection.execute("SELECT * FROM patches WHERE id = ?", (patch_id,)).fetchone()
        return self._patch_from_row(row)

    def update_patch(
        self,
        patch_id: str,
        status: PatchStatus,
        *,
        build_succeeded: bool | None = None,
    ) -> Patch:
        return self.update_patch_status(patch_id, status, build_succeeded=build_succeeded)

    def create_verification(self, verification: Verification) -> Verification:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT patches.* FROM patches WHERE patches.id = ?",
                (verification.patch_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"patch not found: {verification.patch_id}")
            connection.execute(
                """
                INSERT INTO verifications (
                    id, patch_id, attempt_id, result, summary, artifact_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    verification.id,
                    verification.patch_id,
                    verification.attempt_id,
                    verification.result.value,
                    verification.summary,
                    verification.artifact_digest,
                    verification.created_at,
                ),
            )
            if verification.result == VerificationResult.PASS and PatchStatus(row["status"]) == PatchStatus.APPROVED:
                connection.execute(
                    "UPDATE patches SET status = ?, updated_at = ? WHERE id = ?",
                    (PatchStatus.VERIFIED.value, verification.created_at, verification.patch_id),
                )
            self._append_event_row(
                connection,
                Event(
                    run_id=row["run_id"],
                    event_type="verification.created",
                    entity_type="verification",
                    entity_id=verification.id,
                    payload={"patch_id": verification.patch_id, "result": verification.result.value},
                ),
            )
        return verification

    def list_verifications(self, patch_id: str) -> list[Verification]:
        rows = self.connection.execute(
            "SELECT * FROM verifications WHERE patch_id = ? ORDER BY created_at, id",
            (patch_id,),
        ).fetchall()
        return [self._verification_from_row(row) for row in rows]

    def append_event(self, event: Event) -> Event:
        with self.transaction() as connection:
            self._append_event_row(connection, event)
        return event

    def list_events(self, run_id: str) -> list[Event]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY created_at, id",
            (run_id,),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    @staticmethod
    def _append_decision_row(connection: sqlite3.Connection, decision: Decision) -> None:
        connection.execute(
            """
            INSERT INTO decisions (id, target_type, target_id, decision, reason, actor, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.id,
                decision.target_type,
                decision.target_id,
                decision.decision,
                decision.reason,
                decision.actor,
                decision.created_at,
            ),
        )

    @staticmethod
    def _append_event_row(connection: sqlite3.Connection, event: Event) -> None:
        connection.execute(
            """
            INSERT INTO events (
                id, run_id, event_type, entity_type, entity_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.run_id,
                event.event_type,
                event.entity_type,
                event.entity_id,
                canonical_json(event.payload),
                event.created_at,
            ),
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> Run:
        return Run(
            id=row["id"],
            repository=row["repository"],
            commit_sha=row["commit_sha"],
            tree_sha=row["tree_sha"],
            profile=row["profile"],
            status=RunStatus(row["status"]),
            config_digest=row["config_digest"],
            frozen_config=json.loads(row["frozen_config_json"]),
            budget_usd=row["budget_usd"],
            estimated_cost_usd=row["estimated_cost_usd"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error=row["error"],
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            run_id=row["run_id"],
            stage=row["stage"],
            role=AgentRole(row["role"]),
            route=row["route"],
            input_digest=row["input_digest"],
            status=TaskStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _attempt_values(attempt: Attempt) -> tuple[Any, ...]:
        return (
            attempt.id,
            attempt.task_id,
            attempt.ordinal,
            attempt.status.value,
            attempt.thread_id,
            attempt.runtime_name,
            attempt.runtime_version,
            attempt.model,
            attempt.model_provider,
            attempt.prompt_digest,
            attempt.schema_digest,
            attempt.bundle_digest,
            attempt.input_tokens,
            attempt.cached_input_tokens,
            attempt.output_tokens,
            attempt.reasoning_tokens,
            attempt.estimated_cost_usd,
            attempt.trace_artifact_digest,
            attempt.output_artifact_digest,
            attempt.duration_ms,
            attempt.error,
            attempt.created_at,
            attempt.completed_at,
        )

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> Attempt:
        return Attempt(
            id=row["id"],
            task_id=row["task_id"],
            ordinal=row["ordinal"],
            status=AttemptStatus(row["status"]),
            thread_id=row["thread_id"],
            runtime_name=row["runtime_name"],
            runtime_version=row["runtime_version"],
            model=row["model"],
            model_provider=row["model_provider"],
            prompt_digest=row["prompt_digest"],
            schema_digest=row["schema_digest"],
            bundle_digest=row["bundle_digest"],
            input_tokens=row["input_tokens"],
            cached_input_tokens=row["cached_input_tokens"],
            output_tokens=row["output_tokens"],
            reasoning_tokens=row["reasoning_tokens"],
            estimated_cost_usd=row["estimated_cost_usd"],
            trace_artifact_digest=row["trace_artifact_digest"],
            output_artifact_digest=row["output_artifact_digest"],
            duration_ms=row["duration_ms"],
            error=row["error"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> Artifact:
        return Artifact(
            digest=row["digest"],
            relative_path=row["relative_path"],
            size=row["size"],
            media_type=row["media_type"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _finding_from_row(row: sqlite3.Row) -> Finding:
        return Finding(
            id=row["id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            attempt_id=row["attempt_id"],
            fingerprint=row["fingerprint"],
            role=AgentRole(row["role"]),
            category=row["category"],
            severity=FindingSeverity(row["severity"]),
            title=row["title"],
            claim=row["claim"],
            evidence=tuple(json.loads(row["evidence_json"])),
            explanation=row["explanation"],
            suggested_action=row["suggested_action"],
            confidence=row["confidence"],
            status=FindingStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> Decision:
        return Decision(
            id=row["id"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            decision=row["decision"],
            reason=row["reason"],
            actor=row["actor"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _patch_from_row(row: sqlite3.Row) -> Patch:
        return Patch(
            id=row["id"],
            run_id=row["run_id"],
            base_commit=row["base_commit"],
            diff_digest=row["diff_digest"],
            summary=row["summary"],
            edits=tuple(json.loads(row["edits_json"])),
            attempt_id=row["attempt_id"],
            status=PatchStatus(row["status"]),
            build_succeeded=bool(row["build_succeeded"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            applied_at=row["applied_at"],
        )

    @staticmethod
    def _verification_from_row(row: sqlite3.Row) -> Verification:
        return Verification(
            id=row["id"],
            patch_id=row["patch_id"],
            attempt_id=row["attempt_id"],
            result=VerificationResult(row["result"]),
            summary=row["summary"],
            artifact_digest=row["artifact_digest"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            run_id=row["run_id"],
            event_type=row["event_type"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
        )
