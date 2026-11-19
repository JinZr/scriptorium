from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Any, Mapping
import uuid


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class RunStatus(str, Enum):
    PREPARING = "preparing"
    REVIEWING = "reviewing"
    AWAITING_DECISION = "awaiting_decision"
    PLANNING = "planning"
    WRITING = "writing"
    CHECKING = "checking"
    AWAITING_PATCH_APPROVAL = "awaiting_patch_approval"
    READY_TO_APPLY = "ready_to_apply"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class AttemptStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class FindingStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    WAIVED = "waived"


class FindingSeverity(str, Enum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MODERATE = "moderate"
    MINOR = "minor"
    SUGGESTION = "suggestion"


class PatchStatus(str, Enum):
    PROPOSED = "proposed"
    RETURNED = "returned"
    CHECKED = "checked"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    STALE = "stale"


class CheckResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"


class VenueFamily(str, Enum):
    ML_CONFERENCE = "ml_conference"
    NATURE_FAMILY = "nature_family"
    OTHER = "other"


class ReviewStage(str, Enum):
    INTERNAL_DRAFT = "internal_draft"
    PRESUBMISSION = "presubmission"
    REBUTTAL_REVISION = "rebuttal_revision"
    CAMERA_READY = "camera_ready"


class AgentRole(str, Enum):
    WORKFLOW = "workflow"
    SUBSTANTIVE_REVIEW = "substantive_review"
    COPYEDIT = "copyedit"
    CONSISTENCY = "consistency"
    FIGURE_REVIEW = "figure_review"
    WRITING = "writing"
    CHECK = "check"


@dataclass(frozen=True)
class RoleSpec:
    key: AgentRole
    display_name: str
    description: str
    model_backed: bool = True


ROLE_CATALOG: Mapping[AgentRole, RoleSpec] = {
    AgentRole.WORKFLOW: RoleSpec(
        key=AgentRole.WORKFLOW,
        display_name="Armarius",
        description="Deterministic workflow coordinator",
        model_backed=False,
    ),
    AgentRole.SUBSTANTIVE_REVIEW: RoleSpec(
        key=AgentRole.SUBSTANTIVE_REVIEW,
        display_name="Scholiast",
        description="Claims, evidence, methods, and scientific review",
    ),
    AgentRole.COPYEDIT: RoleSpec(
        key=AgentRole.COPYEDIT,
        display_name="Corrector",
        description="Language and copy editing",
    ),
    AgentRole.CONSISTENCY: RoleSpec(
        key=AgentRole.CONSISTENCY,
        display_name="Collator",
        description="Cross-section, terminology, and numeric consistency",
    ),
    AgentRole.FIGURE_REVIEW: RoleSpec(
        key=AgentRole.FIGURE_REVIEW,
        display_name="Figure Reviewer",
        description="Figure, table, caption, and manuscript correspondence",
    ),
    AgentRole.WRITING: RoleSpec(
        key=AgentRole.WRITING,
        display_name="Scribe",
        description="Rewrites the passages one assignment of an approved plan names",
    ),
    AgentRole.CHECK: RoleSpec(
        key=AgentRole.CHECK,
        display_name="Checker",
        description="The lead's check of a candidate patch against the approved plan, assignment by assignment",
    ),
}


RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PREPARING: frozenset({RunStatus.REVIEWING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.REVIEWING: frozenset({RunStatus.AWAITING_DECISION, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.AWAITING_DECISION: frozenset(
        {RunStatus.REVIEWING, RunStatus.PLANNING, RunStatus.COMPLETED, RunStatus.CANCELLED}
    ),
    RunStatus.PLANNING: frozenset({RunStatus.WRITING, RunStatus.COMPLETED, RunStatus.CANCELLED}),
    RunStatus.WRITING: frozenset(
        {RunStatus.CHECKING, RunStatus.PLANNING, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.CHECKING: frozenset(
        {
            RunStatus.AWAITING_PATCH_APPROVAL,
            RunStatus.WRITING,
            RunStatus.PLANNING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.AWAITING_PATCH_APPROVAL: frozenset(
        {RunStatus.PLANNING, RunStatus.READY_TO_APPLY, RunStatus.COMPLETED, RunStatus.CANCELLED}
    ),
    RunStatus.READY_TO_APPLY: frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset({RunStatus.PREPARING, RunStatus.REVIEWING, RunStatus.WRITING, RunStatus.CHECKING}),
    RunStatus.CANCELLED: frozenset(),
}


TASK_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.INTERRUPTED, TaskStatus.CANCELLED}
    ),
    TaskStatus.INTERRUPTED: frozenset({TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.FAILED: frozenset({TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset({TaskStatus.PENDING}),
    TaskStatus.CANCELLED: frozenset(),
}


def validate_run_transition(current: RunStatus, target: RunStatus) -> None:
    if current != target and target not in RUN_TRANSITIONS[current]:
        raise ValueError(f"invalid run status transition: {current.value} -> {target.value}")


def validate_task_transition(current: TaskStatus, target: TaskStatus) -> None:
    if current != target and target not in TASK_TRANSITIONS[current]:
        raise ValueError(f"invalid task status transition: {current.value} -> {target.value}")


@dataclass(frozen=True)
class Run:
    repository: str
    commit_sha: str
    tree_sha: str
    profile: str
    config_digest: str
    frozen_config: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("run"))
    status: RunStatus = RunStatus.PREPARING
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    error: str | None = None
    # Digest of the frozen review brief artifact; runs started without a brief keep None.
    brief_digest: str | None = None


@dataclass(frozen=True)
class Task:
    run_id: str
    stage: str
    role: AgentRole
    input_digest: str
    id: str = field(default_factory=lambda: new_id("task"))
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Attempt:
    task_id: str
    ordinal: int
    id: str = field(default_factory=lambda: new_id("attempt"))
    status: AttemptStatus = AttemptStatus.RUNNING
    thread_id: str | None = None
    model: str | None = None
    external_client: str | None = None
    effort: str | None = None
    session_source: str | None = None
    prompt_digest: str | None = None
    schema_digest: str | None = None
    bundle_digest: str | None = None
    output_artifact_digest: str | None = None
    validation_report_artifact_digest: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=utc_now)
    completed_at: str | None = None


@dataclass(frozen=True)
class Artifact:
    digest: str
    relative_path: str
    size: int
    media_type: str = "application/octet-stream"
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Finding:
    run_id: str
    task_id: str
    attempt_id: str
    fingerprint: str
    role: AgentRole
    category: str
    severity: FindingSeverity
    title: str
    claim: str
    evidence: tuple[Mapping[str, Any], ...]
    explanation: str
    suggested_action: str
    confidence: float
    consequence: str
    # Detail-role findings name the claim they change at moderate or above; substantive findings link claims through
    # their claim checks and leave this empty.
    affected_claim: str | None = None
    id: str = field(default_factory=lambda: new_id("finding"))
    status: FindingStatus = FindingStatus.PENDING
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Decision:
    target_type: str
    target_id: str
    decision: str
    reason: str
    actor: str = "user"
    id: str = field(default_factory=lambda: new_id("decision"))
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Plan:
    """An author-approved plan: the plan text and one assignment per writing task, each bound by artifact digest.

    Each assignment is ``{"id", "artifact_digest", "targets": [{"path", "start_line", "end_line"}], "finding_ids"}``.
    A run's latest plan is its current one.
    """

    run_id: str
    digest: str
    plan_artifact_digest: str
    assignments: tuple[Mapping[str, Any], ...]
    id: str = field(default_factory=lambda: new_id("plan"))
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Patch:
    run_id: str
    base_commit: str
    diff_digest: str
    summary: str
    edits: tuple[Mapping[str, Any], ...]
    id: str = field(default_factory=lambda: new_id("patch"))
    status: PatchStatus = PatchStatus.PROPOSED
    build_succeeded: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    applied_at: str | None = None
    # The plan whose accepted writing outputs this patch was built from.
    plan_id: str | None = None
    # The writing attempt each assignment's edits came from: ``{"assignment_id", "attempt_id"}`` in plan order.
    sources: tuple[Mapping[str, Any], ...] = ()
    # The fact-change list artifact: a reading aid from the navigation index, never evidence.
    facts_digest: str | None = None


@dataclass(frozen=True)
class Check:
    """The lead's check of one candidate patch against its approved plan.

    Each assignment verdict is ``{"assignment_id", "writing_attempt_id", "verdict", "feedback"}``: the writing attempt
    is the one the patch carried for that assignment, so a ``redo`` returns exactly that output.
    """

    patch_id: str
    result: CheckResult
    summary: str
    assignments: tuple[Mapping[str, Any], ...]
    attempt_id: str | None = None
    artifact_digest: str | None = None
    id: str = field(default_factory=lambda: new_id("check"))
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Event:
    run_id: str
    event_type: str
    entity_type: str
    entity_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("event"))
    created_at: str = field(default_factory=utc_now)
