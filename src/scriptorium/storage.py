from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable, Iterator, Mapping, Sequence

from scriptorium.artifacts import ArtifactError, ArtifactStore
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
    Plan,
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

STATE_FILE = "state.json"
LOCK_FILE = "state.lock"
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9_-]+")
_SEVERITY_ORDER = {"blocker": 0, "major": 1, "moderate": 2, "minor": 3}
_FINDING_DECISIONS = {
    "confirm": FindingStatus.CONFIRMED,
    "reject": FindingStatus.REJECTED,
    "waive": FindingStatus.WAIVED,
}
_PATCH_DECISIONS = {"approve": PatchStatus.APPROVED, "reject": PatchStatus.REJECTED}
_DECIDABLE_PATCHES = {PatchStatus.PROPOSED, PatchStatus.APPROVED, PatchStatus.REJECTED}
_PATCH_TRANSITIONS = {
    PatchStatus.PROPOSED: {PatchStatus.APPROVED, PatchStatus.REJECTED},
    PatchStatus.APPROVED: {PatchStatus.REJECTED, PatchStatus.VERIFIED, PatchStatus.STALE},
    PatchStatus.REJECTED: set(),
    PatchStatus.VERIFIED: {PatchStatus.APPLIED, PatchStatus.STALE},
    PatchStatus.APPLIED: set(),
    PatchStatus.STALE: set(),
}
_INCOMPLETE_TASKS = {TaskStatus.PENDING, TaskStatus.FAILED, TaskStatus.INTERRUPTED}
_EXTERNAL_TASK_KEYS = (
    "task_id",
    "prompt_digest",
    "schema_digest",
    "bundle_digest",
    "bundle_path",
    "schema_kind",
    "plan_id",
    "assignment_id",
    "example_digest",
)
# Every key but the task ID and the example digest is frozen task material and must never change.
_FROZEN_EXTERNAL_TASK_KEYS = _EXTERNAL_TASK_KEYS[1:-1]
_ORPHANED_ATTEMPT_ERROR = "process exited before attempt completion"


class StorageError(RuntimeError):
    pass


class NotFoundError(StorageError):
    pass


class ConflictError(StorageError):
    pass


class _RunState:
    """One run's complete durable record, read from and replaced as a single state file."""

    KEYS = (
        "run",
        "tasks",
        "external_tasks",
        "attempts",
        "findings",
        "decisions",
        "plans",
        "patches",
        "verifications",
        "events",
    )

    def __init__(self, run: Run) -> None:
        self.run = run
        self.tasks: dict[str, Task] = {}
        self.external_tasks: dict[str, dict[str, Any]] = {}
        self.attempts: dict[str, Attempt] = {}
        self.findings: dict[str, Finding] = {}
        self.decisions: list[Decision] = []
        self.plans: dict[str, Plan] = {}
        self.patches: dict[str, Patch] = {}
        self.verifications: dict[str, Verification] = {}
        self.events: list[Event] = []
        self.dirty = False

    @classmethod
    def parse(cls, path: Path, data: bytes) -> _RunState:
        try:
            raw = json.loads(data)
            if not isinstance(raw, dict) or set(raw) != set(cls.KEYS):
                raise ValueError("unexpected top-level keys")
            state = cls(_run_from(raw["run"]))
            state.tasks = {item.id: item for item in map(_task_from, raw["tasks"])}
            state.external_tasks = {item["task_id"]: _external_task_from(item) for item in raw["external_tasks"]}
            state.attempts = {item.id: item for item in map(_attempt_from, raw["attempts"])}
            state.findings = {item.id: item for item in map(_finding_from, raw["findings"])}
            state.decisions = [Decision(**item) for item in raw["decisions"]]
            state.plans = {item.id: item for item in map(_plan_from, raw["plans"])}
            state.patches = {item.id: item for item in map(_patch_from, raw["patches"])}
            state.verifications = {item.id: item for item in map(_verification_from, raw["verifications"])}
            state.events = [Event(**item) for item in raw["events"]]
            if state.run.id != path.parent.name:
                raise ValueError(f"run {state.run.id} does not match its directory")
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageError(f"run state is unreadable: {path}: {exc}") from exc
        return state

    def serialize(self) -> bytes:
        document = {
            "run": vars(self.run),
            "tasks": [vars(item) for item in self.tasks.values()],
            "external_tasks": list(self.external_tasks.values()),
            "attempts": [vars(item) for item in self.attempts.values()],
            "findings": [vars(item) for item in self.findings.values()],
            "decisions": [vars(item) for item in self.decisions],
            "plans": [vars(item) for item in self.plans.values()],
            "patches": [vars(item) for item in self.patches.values()],
            "verifications": [vars(item) for item in self.verifications.values()],
            "events": [vars(item) for item in self.events],
        }
        return canonical_json(document).encode("utf-8")

    def add_event(self, event: Event) -> None:
        if any(item.id == event.id for item in self.events):
            raise ConflictError(f"event already exists: {event.id}")
        self.events.append(event)
        self.dirty = True

    def add_decision(self, decision: Decision) -> None:
        if any(item.id == decision.id for item in self.decisions):
            raise ConflictError(f"decision already exists: {decision.id}")
        self.decisions.append(decision)
        self.dirty = True

    def set_run(self, run: Run) -> None:
        self.run = run
        self.dirty = True

    def set_task(self, task: Task) -> None:
        self.tasks[task.id] = task
        self.dirty = True

    def set_attempt(self, attempt: Attempt) -> None:
        current = self.attempts.get(attempt.id)
        if current is not None and current.status != AttemptStatus.RUNNING:
            raise ConflictError(f"terminal attempts are immutable: {attempt.id}")
        self.attempts[attempt.id] = attempt
        self.dirty = True

    def set_finding(self, finding: Finding) -> None:
        self.findings[finding.id] = finding
        self.dirty = True

    def set_patch(self, patch: Patch) -> None:
        self.patches[patch.id] = patch
        self.dirty = True

    def task_status_event(self, task: Task, status: TaskStatus) -> Event:
        return Event(
            run_id=self.run.id,
            event_type="task.status_changed",
            entity_type="task",
            entity_id=task.id,
            payload={"from": task.status.value, "to": status.value},
        )

    def interrupt_running_attempts(self, at: str) -> int:
        running = sorted(
            (item for item in self.attempts.values() if item.status == AttemptStatus.RUNNING),
            key=lambda item: (item.created_at, item.id),
        )
        for attempt in running:
            self.set_attempt(
                replace(attempt, status=AttemptStatus.INTERRUPTED, completed_at=at, error=_ORPHANED_ATTEMPT_ERROR)
            )
            self.set_task(replace(self.tasks[attempt.task_id], status=TaskStatus.INTERRUPTED, updated_at=at))
            self.add_event(
                Event(
                    run_id=self.run.id,
                    event_type="attempt.interrupted",
                    entity_type="attempt",
                    entity_id=attempt.id,
                    payload={"task_id": attempt.task_id, "ordinal": attempt.ordinal},
                )
            )
        return len(running)

    def cancel_incomplete_tasks(self, at: str) -> int:
        incomplete = sorted(
            (item for item in self.tasks.values() if item.status in _INCOMPLETE_TASKS),
            key=lambda item: (item.created_at, item.id),
        )
        for task in incomplete:
            validate_task_transition(task.status, TaskStatus.CANCELLED)
            self.set_task(replace(task, status=TaskStatus.CANCELLED, updated_at=at))
            self.add_event(self.task_status_event(task, TaskStatus.CANCELLED))
        return len(incomplete)

    def record_finding_decision(self, decision: Decision) -> None:
        finding = self.findings[decision.target_id]
        status = _FINDING_DECISIONS[decision.decision]
        self.set_finding(replace(finding, status=status, updated_at=decision.created_at))
        self.add_decision(decision)
        self.add_event(self._decision_event("finding", decision))

    def record_patch_decision(self, decision: Decision) -> None:
        patch = self.patches[decision.target_id]
        if patch.status not in _DECIDABLE_PATCHES:
            raise ConflictError(f"patch cannot be decided in status {patch.status.value}")
        status = _PATCH_DECISIONS[decision.decision]
        self.set_patch(replace(patch, status=status, updated_at=decision.created_at))
        self.add_decision(decision)
        self.add_event(self._decision_event("patch", decision))

    def record_plan_approval(self, plan: Plan, decision: Decision) -> None:
        self.plans[plan.id] = plan
        self.add_decision(decision)
        self.add_event(self._decision_event("plan", decision))

    def _decision_event(self, kind: str, decision: Decision) -> Event:
        return Event(
            run_id=self.run.id,
            event_type=f"{kind}.decided",
            entity_type=kind,
            entity_id=decision.target_id,
            payload={"decision": decision.decision, "reason": decision.reason, "actor": decision.actor},
        )


class RunStore:
    """Durable workflow state: one JSON state file per run, replaced atomically under a per-run file lock.

    ``path`` is the state directory. Run ``RUN_ID`` exists exactly when ``runs/RUN_ID/state.json`` exists. Every
    mutation takes the run's exclusive ``state.lock``, loads the state, changes it in memory, and replaces the file
    with a complete, fsynced copy, so a reader or a crash sees the old state or the new one, never a mixture.
    """

    def __init__(self, path: str | Path) -> None:
        self.root = Path(path)
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._artifacts = ArtifactStore(self.root / "artifacts")

    def close(self) -> None:
        pass

    def __enter__(self) -> RunStore:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    # Runs

    def create_run(self, run: Run) -> Run:
        if not _SAFE_RUN_ID.fullmatch(run.id):
            raise StorageError(f"unsafe run id: {run.id!r}")
        if run.brief_digest is not None:
            self._require_artifact(run.brief_digest)
        path = self._state_path(run.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._run_lock(run.id):
            if path.exists():
                raise ConflictError(f"run already exists: {run.id}")
            state = _RunState(run)
            state.add_event(
                Event(
                    run_id=run.id,
                    event_type="run.created",
                    entity_type="run",
                    entity_id=run.id,
                    payload={"status": run.status.value},
                )
            )
            self._write(path, state)
        return run

    def get_run(self, run_id: str) -> Run:
        return self._load(run_id).run

    def list_runs(self) -> list[Run]:
        return sorted((state.run for state in self._states()), key=lambda run: (run.created_at, run.id))

    def list_run_summaries(self, status: RunStatus | None, limit: int) -> tuple[int, list[tuple[Run, dict[str, int]]]]:
        """Return the matching run count and the newest ``limit`` runs with their task counts by status."""
        states = sorted(
            (state for state in self._states() if status is None or state.run.status == status),
            key=lambda state: (state.run.created_at, state.run.id),
            reverse=True,
        )
        rows = []
        for state in states[:limit]:
            counts: dict[str, int] = {}
            for task in state.tasks.values():
                counts[task.status.value] = counts.get(task.status.value, 0) + 1
            rows.append((state.run, dict(sorted(counts.items()))))
        return len(states), rows

    def list_active_runs(self, commit_sha: str) -> list[Run]:
        # A failed run can still be resumed, so only completed and cancelled runs are inactive.
        inactive = {RunStatus.COMPLETED, RunStatus.CANCELLED}
        return [run for run in self.list_runs() if run.commit_sha == commit_sha and run.status not in inactive]

    def update_run_status(self, run_id: str, status: RunStatus, error: str | None = None) -> Run:
        with self._mutation(run_id) as state:
            current = state.run.status
            validate_run_transition(current, status)
            if current == status and state.run.error == error:
                return state.run
            state.set_run(replace(state.run, status=status, error=error, updated_at=utc_now()))
            state.add_event(
                Event(
                    run_id=run_id,
                    event_type="run.status_changed",
                    entity_type="run",
                    entity_id=run_id,
                    payload={"from": current.value, "to": status.value, "error": error},
                )
            )
            return state.run

    def update_run(self, run_id: str, status: RunStatus, error: str | None = None) -> Run:
        return self.update_run_status(run_id, status, error)

    def cancel_run(self, run_id: str, reason: str, request_id: str) -> Run:
        with self._mutation(run_id) as state:
            current = state.run.status
            if current == RunStatus.CANCELLED:
                return state.run
            if current in {RunStatus.COMPLETED, RunStatus.FAILED}:
                raise ConflictError(f"run cannot be cancelled while {current.value}")
            updated_at = utc_now()
            state.interrupt_running_attempts(updated_at)
            state.cancel_incomplete_tasks(updated_at)
            validate_run_transition(current, RunStatus.CANCELLED)
            state.set_run(replace(state.run, status=RunStatus.CANCELLED, error=None, updated_at=updated_at))
            state.add_event(
                Event(
                    run_id=run_id,
                    event_type="run.status_changed",
                    entity_type="run",
                    entity_id=run_id,
                    payload={"from": current.value, "to": RunStatus.CANCELLED.value, "error": None},
                )
            )
            state.add_event(
                Event(
                    run_id=run_id,
                    event_type="run.cancelled",
                    entity_type="run",
                    entity_id=run_id,
                    payload={"reason": reason, "request_id": request_id},
                )
            )
            return state.run

    def cancel_incomplete_tasks(self, run_id: str) -> int:
        if not self._exists(run_id):
            return 0
        with self._mutation(run_id) as state:
            return state.cancel_incomplete_tasks(utc_now())

    def recover_orphaned_attempts(self, run_id: str) -> int:
        # Callers must already own the run's exclusive flock; the state file alone cannot prove an owner is dead.
        if not self._exists(run_id):
            return 0
        with self._mutation(run_id) as state:
            return state.interrupt_running_attempts(utc_now())

    # Tasks

    def create_task(self, task: Task) -> Task:
        with self._mutation(task.run_id) as state:
            if task.id in state.tasks:
                raise ConflictError(f"task already exists: {task.id}")
            key = (task.stage, task.role, task.input_digest)
            if any((item.stage, item.role, item.input_digest) == key for item in state.tasks.values()):
                raise ConflictError("task already exists for run, stage, role, and input digest")
            state.set_task(task)
            state.add_event(
                Event(
                    run_id=task.run_id,
                    event_type="task.created",
                    entity_type="task",
                    entity_id=task.id,
                    payload={"stage": task.stage, "role": task.role.value},
                )
            )
        return task

    def get_task(self, task_id: str) -> Task:
        return self._owner(task_id, "task", _tasks).tasks[task_id]

    def find_task(self, run_id: str, stage: str, role: AgentRole, input_digest: str) -> Task | None:
        state = self._load_if_present(run_id)
        if state is None:
            return None
        key = (stage, role, input_digest)
        return next((item for item in state.tasks.values() if (item.stage, item.role, item.input_digest) == key), None)

    def list_tasks(self, run_id: str) -> list[Task]:
        state = self._load_if_present(run_id)
        return [] if state is None else sorted(state.tasks.values(), key=lambda item: (item.created_at, item.id))

    def update_task_status(self, task_id: str, status: TaskStatus) -> Task:
        with self._mutation(self._owner(task_id, "task", _tasks).run.id) as state:
            task = state.tasks[task_id]
            validate_task_transition(task.status, status)
            if task.status == status:
                return task
            state.set_task(replace(task, status=status, updated_at=utc_now()))
            state.add_event(state.task_status_event(task, status))
            return state.tasks[task_id]

    def record_external_task(
        self,
        task_id: str,
        prompt_digest: str,
        schema_digest: str,
        bundle_digest: str,
        bundle_path: str,
        schema_kind: str,
        example_digest: str | None = None,
        *,
        plan_id: str | None = None,
        assignment_id: str | None = None,
    ) -> None:
        # The example digest is written only with the first record; it is not part of the frozen-input identity.
        # A writing task names the plan and assignment it was prepared from.
        record = dict(
            zip(
                _EXTERNAL_TASK_KEYS,
                (
                    task_id,
                    prompt_digest,
                    schema_digest,
                    bundle_digest,
                    bundle_path,
                    schema_kind,
                    plan_id,
                    assignment_id,
                    example_digest,
                ),
            )
        )
        with self._mutation(self._owner(task_id, "task", _tasks).run.id) as state:
            existing = state.external_tasks.get(task_id)
            if existing is None:
                for digest in (prompt_digest, schema_digest, example_digest):
                    if digest is not None:
                        self._require_artifact(digest)
                state.external_tasks[task_id] = existing = record
                state.dirty = True
            if any(existing[key] != record[key] for key in _FROZEN_EXTERNAL_TASK_KEYS):
                raise ConflictError(f"external task material changed: {task_id}")

    def has_external_task(self, task_id: str) -> bool:
        return self._find(task_id, _external_tasks) is not None

    def get_external_task(self, task_id: str) -> dict[str, str]:
        return dict(self._owner(task_id, "external task", _external_tasks).external_tasks[task_id])

    # Attempts

    def create_attempt(self, attempt: Attempt) -> Attempt:
        if attempt.status != AttemptStatus.RUNNING:
            raise ValueError("new attempts must have running status")
        with self._mutation(self._owner(attempt.task_id, "task", _tasks).run.id) as state:
            self._add_attempt(state, attempt)
        return attempt

    def begin_attempt(self, task_id: str, **values: Any) -> Attempt:
        with self._mutation(self._owner(task_id, "task", _tasks).run.id) as state:
            attempt = Attempt(task_id=task_id, ordinal=_next_ordinal(state, task_id), **values)
            if attempt.status != AttemptStatus.RUNNING:
                raise ValueError("new attempts must have running status")
            self._add_attempt(state, attempt)
        return attempt

    def get_attempt(self, attempt_id: str) -> Attempt:
        return self._owner(attempt_id, "attempt", _attempts).attempts[attempt_id]

    def list_attempts(self, task_id: str) -> list[Attempt]:
        state = self._find(task_id, _tasks)
        if state is None:
            return []
        return sorted((item for item in state.attempts.values() if item.task_id == task_id), key=_ordinal)

    def record_attempt_session(self, attempt_id: str, thread_id: str) -> Attempt:
        with self._mutation(self._owner(attempt_id, "attempt", _attempts).run.id) as state:
            attempt = state.attempts[attempt_id]
            if attempt.status != AttemptStatus.RUNNING:
                raise ConflictError(f"attempt is already terminal: {attempt_id}")
            if attempt.thread_id is not None and attempt.thread_id != thread_id:
                raise ConflictError(f"attempt already records a different session: {attempt_id}")
            if attempt.thread_id is None:
                state.set_attempt(replace(attempt, thread_id=thread_id))
            return state.attempts[attempt_id]

    def finish_attempt(
        self,
        attempt_id: str,
        status: AttemptStatus,
        *,
        thread_id: str | None = None,
        model: str | None = None,
        output_artifact_digest: str | None = None,
        validation_report_artifact_digest: str | None = None,
        error: str | None = None,
    ) -> Attempt:
        if status == AttemptStatus.RUNNING:
            raise ValueError("finish status must be terminal")
        with self._mutation(self._owner(attempt_id, "attempt", _attempts).run.id) as state:
            attempt = state.attempts[attempt_id]
            if attempt.status != AttemptStatus.RUNNING:
                raise ConflictError(f"attempt is already terminal: {attempt_id}")
            if attempt.thread_id is not None and thread_id is not None and attempt.thread_id != thread_id:
                raise ConflictError(f"attempt already records a different session: {attempt_id}")
            for digest in (output_artifact_digest, validation_report_artifact_digest):
                if digest is not None:
                    self._require_artifact(digest)
            completed_at = utc_now()
            finished = replace(
                attempt,
                status=status,
                thread_id=attempt.thread_id if thread_id is None else thread_id,
                model=attempt.model if model is None else model,
                output_artifact_digest=output_artifact_digest,
                validation_report_artifact_digest=validation_report_artifact_digest,
                error=error,
                completed_at=completed_at,
            )
            state.set_attempt(finished)
            state.set_task(
                replace(state.tasks[attempt.task_id], status=TaskStatus(status.value), updated_at=completed_at)
            )
            state.set_run(replace(state.run, updated_at=completed_at))
            state.add_event(
                Event(
                    run_id=state.run.id,
                    event_type="attempt.finished",
                    entity_type="attempt",
                    entity_id=attempt_id,
                    payload={"status": status.value, "task_id": attempt.task_id, "ordinal": attempt.ordinal},
                )
            )
        return finished

    def update_attempt(self, attempt_id: str, status: AttemptStatus, **result: Any) -> Attempt:
        return self.finish_attempt(attempt_id, status, **result)

    # Artifacts

    def record_artifact(self, artifact: Artifact) -> Artifact:
        try:
            existing = self._artifacts.record(artifact)
        except (ArtifactError, OSError, ValueError) as exc:
            raise StorageError(f"cannot record artifact {artifact.digest}: {exc}") from exc
        if existing.relative_path != artifact.relative_path or existing.size != artifact.size:
            raise ConflictError(f"artifact metadata conflicts for digest {artifact.digest}")
        return existing

    def add_artifact(self, artifact: Artifact) -> Artifact:
        return self.record_artifact(artifact)

    def get_artifact(self, digest: str) -> Artifact:
        try:
            artifact = self._artifacts.read_record(digest)
        except ValueError:
            artifact = None
        except (ArtifactError, OSError) as exc:
            raise StorageError(str(exc)) from exc
        if artifact is None:
            raise NotFoundError(f"artifact not found: {digest}")
        return artifact

    # Findings and decisions

    def create_finding(self, finding: Finding) -> Finding:
        with self._mutation(finding.run_id) as state:
            _add_finding(state, finding)
        return finding

    def get_or_create_finding(self, finding: Finding) -> Finding:
        with self._mutation(finding.run_id) as state:
            existing = next((item for item in state.findings.values() if item.fingerprint == finding.fingerprint), None)
            if existing is not None:
                return existing
            _add_finding(state, finding)
        return finding

    def fill_finding_affected_claim(self, finding_id: str, affected_claim: str) -> Finding:
        """Set a finding's affected claim only while it is still null; the first non-null value is kept."""
        with self._mutation(self._owner(finding_id, "finding", _findings).run.id) as state:
            finding = state.findings[finding_id]
            if finding.affected_claim is None:
                state.set_finding(replace(finding, affected_claim=affected_claim))
            return state.findings[finding_id]

    def get_finding(self, finding_id: str) -> Finding:
        return self._owner(finding_id, "finding", _findings).findings[finding_id]

    def list_findings(self, run_id: str, statuses: Sequence[FindingStatus] | None = None) -> list[Finding]:
        state = self._load_if_present(run_id)
        if state is None:
            return []
        selected = [item for item in state.findings.values() if not statuses or item.status in statuses]
        return sorted(
            selected, key=lambda item: (_SEVERITY_ORDER.get(item.severity.value, 4), item.created_at, item.id)
        )

    def decide_finding(self, finding_id: str, decision: str, reason: str, actor: str = "user") -> Decision:
        return self.decide_findings([finding_id], decision, reason, actor)[0]

    def decide_findings(
        self, finding_ids: list[str], decision: str, reason: str, actor: str = "user"
    ) -> list[Decision]:
        if decision not in _FINDING_DECISIONS:
            raise ValueError(f"invalid finding decision: {decision}")
        if not reason.strip():
            raise ValueError("decision reason is required")
        if not finding_ids:
            return []
        records = []
        with self._mutation(self._owner(finding_ids[0], "finding", _findings).run.id) as state:
            for finding_id in finding_ids:
                if finding_id not in state.findings:
                    owner = self._find(finding_id, _findings)
                    if owner is None:
                        raise NotFoundError(f"finding not found: {finding_id}")
                    raise ValueError("findings must all belong to the same run")
                record = Decision(
                    target_type="finding", target_id=finding_id, decision=decision, reason=reason, actor=actor
                )
                state.record_finding_decision(record)
                records.append(record)
        return records

    def list_decisions(self, target_type: str, target_id: str) -> list[Decision]:
        state = self._find(target_id, _decision_targets)
        if state is None:
            return []
        selected = [item for item in state.decisions if (item.target_type, item.target_id) == (target_type, target_id)]
        return sorted(selected, key=lambda item: (item.created_at, item.id))

    def append_decision(self, decision: Decision) -> Decision:
        if not decision.reason.strip():
            raise ValueError("decision reason is required")
        if decision.target_type == "finding":
            if decision.decision not in _FINDING_DECISIONS:
                raise ValueError(f"invalid finding decision: {decision.decision}")
            with self._mutation(self._owner(decision.target_id, "finding", _findings).run.id) as state:
                state.record_finding_decision(decision)
            return decision
        if decision.target_type == "patch":
            if decision.decision not in _PATCH_DECISIONS:
                raise ValueError(f"invalid patch decision: {decision.decision}")
            with self._mutation(self._owner(decision.target_id, "patch", _patches).run.id) as state:
                state.record_patch_decision(decision)
            return decision
        raise ValueError(f"decisions target a finding or a patch, not {decision.target_type}")

    # Plans

    def approve_plan(self, plan: Plan, reason: str, actor: str = "user") -> Decision:
        """Record an approved plan, its approval decision and event, and move the run to writing, in one replacement."""
        if not reason.strip():
            raise ValueError("decision reason is required")
        record = Decision(target_type="plan", target_id=plan.id, decision="approve", reason=reason, actor=actor)
        with self._mutation(plan.run_id) as state:
            current = state.run.status
            if current != RunStatus.PLANNING:
                raise ConflictError(f"a plan cannot be approved while the run is {current.value}")
            if plan.id in state.plans:
                raise ConflictError(f"plan already exists: {plan.id}")
            for digest in (plan.plan_artifact_digest, *(item["artifact_digest"] for item in plan.assignments)):
                self._require_artifact(digest)
            state.record_plan_approval(plan, record)
            validate_run_transition(current, RunStatus.WRITING)
            state.set_run(replace(state.run, status=RunStatus.WRITING, error=None, updated_at=utc_now()))
            state.add_event(
                Event(
                    run_id=plan.run_id,
                    event_type="run.status_changed",
                    entity_type="run",
                    entity_id=plan.run_id,
                    payload={"from": current.value, "to": RunStatus.WRITING.value, "error": None},
                )
            )
        return record

    def get_plan(self, plan_id: str) -> Plan:
        return self._owner(plan_id, "plan", _plans).plans[plan_id]

    def list_plans(self, run_id: str) -> list[Plan]:
        state = self._load_if_present(run_id)
        return [] if state is None else sorted(state.plans.values(), key=lambda item: (item.created_at, item.id))

    # Patches and verifications

    def create_patch(self, patch: Patch) -> Patch:
        if patch.plan_id is None:
            raise ValueError("new patches require the plan they were built from")
        with self._mutation(patch.run_id) as state:
            if patch.id in state.patches:
                raise ConflictError(f"patch already exists: {patch.id}")
            key = (patch.base_commit, patch.diff_digest)
            if any((item.base_commit, item.diff_digest) == key for item in state.patches.values()):
                raise ConflictError("patch already exists for run, base commit, and diff digest")
            _require_plan_in(state, patch.plan_id)
            self._require_artifact(patch.diff_digest)
            state.set_patch(patch)
            state.add_event(
                Event(
                    run_id=patch.run_id,
                    event_type="patch.created",
                    entity_type="patch",
                    entity_id=patch.id,
                    payload={
                        "status": patch.status.value,
                        "diff_digest": patch.diff_digest,
                        "plan_id": patch.plan_id,
                    },
                )
            )
        return patch

    def get_patch(self, patch_id: str) -> Patch:
        return self._owner(patch_id, "patch", _patches).patches[patch_id]

    def list_patches(self, run_id: str) -> list[Patch]:
        state = self._load_if_present(run_id)
        return [] if state is None else sorted(state.patches.values(), key=lambda item: (item.updated_at, item.id))

    def repropose_patch(
        self,
        patch_id: str,
        *,
        plan_id: str,
        summary: str,
        edits: Sequence[Mapping[str, Any]],
        build_succeeded: bool,
    ) -> Patch:
        with self._mutation(self._owner(patch_id, "patch", _patches).run.id) as state:
            patch = state.patches[patch_id]
            if patch.status != PatchStatus.REJECTED:
                raise ConflictError(f"patch cannot be reproposed in status {patch.status.value}")
            _require_plan_in(state, plan_id)
            state.set_patch(
                replace(
                    patch,
                    summary=summary,
                    # Stored edits are plain JSON; return them as a later read would.
                    edits=tuple(json.loads(canonical_json(list(edits)))),
                    plan_id=plan_id,
                    status=PatchStatus.PROPOSED,
                    build_succeeded=bool(build_succeeded),
                    updated_at=utc_now(),
                )
            )
            state.add_event(
                Event(
                    run_id=state.run.id,
                    event_type="patch.reproposed",
                    entity_type="patch",
                    entity_id=patch_id,
                    payload={
                        "from": patch.status.value,
                        "to": PatchStatus.PROPOSED.value,
                        "previous_plan_id": patch.plan_id,
                        "plan_id": plan_id,
                        "diff_digest": patch.diff_digest,
                    },
                )
            )
            return state.patches[patch_id]

    def decide_patch(self, patch_id: str, decision: str, reason: str, actor: str = "user") -> Decision:
        if decision not in _PATCH_DECISIONS:
            raise ValueError(f"invalid patch decision: {decision}")
        if not reason.strip():
            raise ValueError("decision reason is required")
        record = Decision(target_type="patch", target_id=patch_id, decision=decision, reason=reason, actor=actor)
        with self._mutation(self._owner(patch_id, "patch", _patches).run.id) as state:
            state.record_patch_decision(record)
        return record

    def update_patch_status(self, patch_id: str, status: PatchStatus, *, build_succeeded: bool | None = None) -> Patch:
        with self._mutation(self._owner(patch_id, "patch", _patches).run.id) as state:
            patch = state.patches[patch_id]
            if patch.status != status and status not in _PATCH_TRANSITIONS[patch.status]:
                raise ValueError(f"invalid patch status transition: {patch.status.value} -> {status.value}")
            updated_at = utc_now()
            state.set_patch(
                replace(
                    patch,
                    status=status,
                    build_succeeded=patch.build_succeeded if build_succeeded is None else bool(build_succeeded),
                    updated_at=updated_at,
                    applied_at=updated_at if status == PatchStatus.APPLIED else patch.applied_at,
                )
            )
            state.add_event(
                Event(
                    run_id=state.run.id,
                    event_type="patch.status_changed",
                    entity_type="patch",
                    entity_id=patch_id,
                    payload={"from": patch.status.value, "to": status.value},
                )
            )
            return state.patches[patch_id]

    def update_patch(self, patch_id: str, status: PatchStatus, *, build_succeeded: bool | None = None) -> Patch:
        return self.update_patch_status(patch_id, status, build_succeeded=build_succeeded)

    def create_verification(self, verification: Verification) -> Verification:
        with self._mutation(self._owner(verification.patch_id, "patch", _patches).run.id) as state:
            if verification.id in state.verifications:
                raise ConflictError(f"verification already exists: {verification.id}")
            if verification.attempt_id is not None:
                _require_attempt_in(state, verification.attempt_id)
            if verification.artifact_digest is not None:
                self._require_artifact(verification.artifact_digest)
            state.verifications[verification.id] = verification
            state.dirty = True
            patch = state.patches[verification.patch_id]
            if verification.result == VerificationResult.PASS and patch.status == PatchStatus.APPROVED:
                state.set_patch(replace(patch, status=PatchStatus.VERIFIED, updated_at=verification.created_at))
            state.add_event(
                Event(
                    run_id=state.run.id,
                    event_type="verification.created",
                    entity_type="verification",
                    entity_id=verification.id,
                    payload={"patch_id": verification.patch_id, "result": verification.result.value},
                )
            )
        return verification

    def list_verifications(self, patch_id: str) -> list[Verification]:
        state = self._find(patch_id, _patches)
        if state is None:
            return []
        selected = [item for item in state.verifications.values() if item.patch_id == patch_id]
        return sorted(selected, key=lambda item: (item.created_at, item.id))

    # Events

    def append_event(self, event: Event) -> Event:
        with self._mutation(event.run_id) as state:
            state.add_event(event)
        return event

    def list_events(self, run_id: str) -> list[Event]:
        state = self._load_if_present(run_id)
        return [] if state is None else sorted(state.events, key=lambda item: (item.created_at, item.id))

    # State files

    def _state_path(self, run_id: str) -> Path:
        return self.runs_dir / run_id / STATE_FILE

    def _exists(self, run_id: str) -> bool:
        return bool(_SAFE_RUN_ID.fullmatch(run_id)) and self._state_path(run_id).is_file()

    def _load(self, run_id: str) -> _RunState:
        state = self._load_if_present(run_id)
        if state is None:
            raise NotFoundError(f"run not found: {run_id}")
        return state

    def _load_if_present(self, run_id: str) -> _RunState | None:
        if not _SAFE_RUN_ID.fullmatch(run_id):
            return None
        path = self._state_path(run_id)
        data = _read(path)
        return None if data is None else _RunState.parse(path, data)

    def _state_paths(self) -> list[Path]:
        try:
            with os.scandir(self.runs_dir) as entries:
                names = sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise StorageError(f"cannot list runs in {self.runs_dir}: {exc}") from exc
        return [self._state_path(name) for name in names if _SAFE_RUN_ID.fullmatch(name)]

    def _states(self) -> Iterator[_RunState]:
        # A run directory without a state file holds no run: its creation never completed.
        for path in self._state_paths():
            data = _read(path)
            if data is not None:
                yield _RunState.parse(path, data)

    def _find(self, entity_id: str, members: Callable[[_RunState], Any]) -> _RunState | None:
        """Scan the run state files for the run whose ``members`` include ``entity_id``."""
        needle = json.dumps(entity_id, ensure_ascii=False).encode("utf-8")
        for path in self._state_paths():
            data = _read(path)
            if data is None or needle not in data:
                continue
            state = _RunState.parse(path, data)
            if entity_id in members(state):
                return state
        return None

    def _owner(self, entity_id: str, kind: str, members: Callable[[_RunState], Any]) -> _RunState:
        state = self._find(entity_id, members)
        if state is None:
            raise NotFoundError(f"{kind} not found: {entity_id}")
        return state

    @contextmanager
    def _mutation(self, run_id: str) -> Iterator[_RunState]:
        """Load one run under its lock; replace its state file once if the body changed it and returned normally."""
        if not self._exists(run_id):
            raise NotFoundError(f"run not found: {run_id}")
        with self._run_lock(run_id):
            state = self._load(run_id)
            yield state
            if state.dirty:
                self._write(self._state_path(run_id), state)

    @contextmanager
    def _run_lock(self, run_id: str) -> Iterator[None]:
        # The store's lock is innermost: it is never held while waiting for any other lock.
        path = self.runs_dir / run_id / LOCK_FILE
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise StorageError(f"cannot open run state lock {path}: {exc}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise StorageError(f"unsafe run state lock: {path}")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _write(path: Path, state: _RunState) -> None:
        data = state.serialize()
        try:
            descriptor, name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=path.parent)
        except OSError as exc:
            raise StorageError(f"cannot write run state {path}: {exc}") from exc
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise StorageError(f"cannot write run state {path}: {exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _require_artifact(self, digest: str) -> None:
        try:
            recorded = self._artifacts.record_path(digest).is_file()
        except ValueError:
            recorded = False
        if not recorded:
            raise StorageError(f"artifact is not recorded: {digest}")

    def _add_attempt(self, state: _RunState, attempt: Attempt) -> None:
        task = state.tasks[attempt.task_id]
        if task.status == TaskStatus.RUNNING:
            raise ConflictError(f"task already has a running attempt: {attempt.task_id}")
        validate_task_transition(task.status, TaskStatus.RUNNING)
        expected = _next_ordinal(state, attempt.task_id)
        if attempt.ordinal != expected:
            raise ConflictError(f"attempt ordinal must be {expected} for task {attempt.task_id}, got {attempt.ordinal}")
        if attempt.id in state.attempts:
            raise ConflictError(f"attempt already exists: {attempt.id}")
        for digest in (attempt.output_artifact_digest, attempt.validation_report_artifact_digest):
            if digest is not None:
                self._require_artifact(digest)
        state.set_attempt(attempt)
        state.set_task(replace(task, status=TaskStatus.RUNNING, updated_at=utc_now()))
        state.add_event(
            Event(
                run_id=state.run.id,
                event_type="attempt.started",
                entity_type="attempt",
                entity_id=attempt.id,
                payload={"task_id": attempt.task_id, "ordinal": attempt.ordinal},
            )
        )


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StorageError(f"cannot read run state {path}: {exc}") from exc


def _tasks(state: _RunState) -> Mapping[str, Task]:
    return state.tasks


def _external_tasks(state: _RunState) -> Mapping[str, dict[str, Any]]:
    return state.external_tasks


def _attempts(state: _RunState) -> Mapping[str, Attempt]:
    return state.attempts


def _findings(state: _RunState) -> Mapping[str, Finding]:
    return state.findings


def _patches(state: _RunState) -> Mapping[str, Patch]:
    return state.patches


def _plans(state: _RunState) -> Mapping[str, Plan]:
    return state.plans


def _decision_targets(state: _RunState) -> set[str]:
    return {item.target_id for item in state.decisions}


def _ordinal(attempt: Attempt) -> int:
    return attempt.ordinal


def _next_ordinal(state: _RunState, task_id: str) -> int:
    return max((item.ordinal for item in state.attempts.values() if item.task_id == task_id), default=0) + 1


def _require_attempt_in(state: _RunState, attempt_id: str) -> None:
    if attempt_id not in state.attempts:
        raise StorageError(f"attempt is not recorded for run {state.run.id}: {attempt_id}")


def _require_plan_in(state: _RunState, plan_id: str) -> None:
    if plan_id not in state.plans:
        raise StorageError(f"plan is not recorded for run {state.run.id}: {plan_id}")


def _add_finding(state: _RunState, finding: Finding) -> None:
    if finding.id in state.findings:
        raise ConflictError(f"finding already exists: {finding.id}")
    if any(item.fingerprint == finding.fingerprint for item in state.findings.values()):
        raise ConflictError(f"finding already exists for fingerprint {finding.fingerprint}")
    if finding.task_id not in state.tasks:
        raise StorageError(f"task is not recorded for run {state.run.id}: {finding.task_id}")
    _require_attempt_in(state, finding.attempt_id)
    state.set_finding(finding)
    state.add_event(
        Event(
            run_id=finding.run_id,
            event_type="finding.created",
            entity_type="finding",
            entity_id=finding.id,
            payload={"severity": finding.severity.value, "role": finding.role.value},
        )
    )


def _run_from(record: dict[str, Any]) -> Run:
    return Run(**{**record, "status": RunStatus(record["status"])})


def _task_from(record: dict[str, Any]) -> Task:
    return Task(**{**record, "role": AgentRole(record["role"]), "status": TaskStatus(record["status"])})


def _external_task_from(record: dict[str, Any]) -> dict[str, Any]:
    if set(record) != set(_EXTERNAL_TASK_KEYS):
        raise ValueError("unexpected external task keys")
    return {key: record[key] for key in _EXTERNAL_TASK_KEYS}


def _attempt_from(record: dict[str, Any]) -> Attempt:
    return Attempt(**{**record, "status": AttemptStatus(record["status"])})


def _finding_from(record: dict[str, Any]) -> Finding:
    return Finding(
        **{
            **record,
            "role": AgentRole(record["role"]),
            "severity": FindingSeverity(record["severity"]),
            "status": FindingStatus(record["status"]),
            "evidence": tuple(record["evidence"]),
            "confidence": float(record["confidence"]),
        }
    )


def _plan_from(record: dict[str, Any]) -> Plan:
    return Plan(**{**record, "assignments": tuple(record["assignments"])})


def _patch_from(record: dict[str, Any]) -> Patch:
    return Patch(
        **{
            **record,
            "status": PatchStatus(record["status"]),
            "edits": tuple(record["edits"]),
            "build_succeeded": bool(record["build_succeeded"]),
        }
    )


def _verification_from(record: dict[str, Any]) -> Verification:
    return Verification(**{**record, "result": VerificationResult(record["result"])})
