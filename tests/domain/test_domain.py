import pytest

from scriptorium.domain import (
    ROLE_CATALOG,
    AgentRole,
    RunStatus,
    TaskStatus,
    digest_json,
    validate_run_transition,
    validate_task_transition,
)


def test_role_catalog_uses_stable_keys_and_display_names() -> None:
    assert set(ROLE_CATALOG) == set(AgentRole)
    assert ROLE_CATALOG[AgentRole.WORKFLOW].display_name == "Armarius"
    assert ROLE_CATALOG[AgentRole.WORKFLOW].model_backed is False
    assert ROLE_CATALOG[AgentRole.SUBSTANTIVE_REVIEW].display_name == "Scholiast"
    assert ROLE_CATALOG[AgentRole.COPYEDIT].display_name == "Corrector"
    assert ROLE_CATALOG[AgentRole.CONSISTENCY].display_name == "Collator"
    assert ROLE_CATALOG[AgentRole.FIGURE_REVIEW].display_name == "Figure Reviewer"
    assert ROLE_CATALOG[AgentRole.WRITING].display_name == "Scribe"
    assert ROLE_CATALOG[AgentRole.CHECK].display_name == "Checker"


def test_canonical_json_digest_is_order_independent() -> None:
    assert digest_json({"b": 2, "a": [1, 3]}) == digest_json({"a": [1, 3], "b": 2})
    assert digest_json({"a": 1}) != digest_json({"a": 2})


def test_run_status_transitions_are_explicit() -> None:
    validate_run_transition(RunStatus.PREPARING, RunStatus.REVIEWING)
    validate_run_transition(RunStatus.READY_TO_APPLY, RunStatus.COMPLETED)

    with pytest.raises(ValueError, match="invalid run status transition"):
        validate_run_transition(RunStatus.PREPARING, RunStatus.COMPLETED)


def test_planning_writing_and_the_check_sit_between_the_decisions_and_patch_approval() -> None:
    for current, target in (
        (RunStatus.AWAITING_DECISION, RunStatus.PLANNING),
        (RunStatus.PLANNING, RunStatus.WRITING),
        (RunStatus.WRITING, RunStatus.CHECKING),
        (RunStatus.WRITING, RunStatus.PLANNING),
        (RunStatus.CHECKING, RunStatus.AWAITING_PATCH_APPROVAL),
        (RunStatus.CHECKING, RunStatus.WRITING),
        (RunStatus.CHECKING, RunStatus.PLANNING),
        (RunStatus.AWAITING_PATCH_APPROVAL, RunStatus.READY_TO_APPLY),
        (RunStatus.AWAITING_PATCH_APPROVAL, RunStatus.PLANNING),
        (RunStatus.FAILED, RunStatus.WRITING),
        (RunStatus.FAILED, RunStatus.CHECKING),
    ):
        validate_run_transition(current, target)
    for current, target in (
        (RunStatus.AWAITING_DECISION, RunStatus.WRITING),
        (RunStatus.PLANNING, RunStatus.AWAITING_PATCH_APPROVAL),
        (RunStatus.WRITING, RunStatus.AWAITING_PATCH_APPROVAL),
        (RunStatus.AWAITING_PATCH_APPROVAL, RunStatus.CHECKING),
        (RunStatus.CHECKING, RunStatus.READY_TO_APPLY),
        (RunStatus.FAILED, RunStatus.PLANNING),
    ):
        with pytest.raises(ValueError, match="invalid run status transition"):
            validate_run_transition(current, target)


def test_task_can_retry_only_after_failure_or_interruption() -> None:
    validate_task_transition(TaskStatus.PENDING, TaskStatus.RUNNING)
    validate_task_transition(TaskStatus.INTERRUPTED, TaskStatus.RUNNING)
    validate_task_transition(TaskStatus.FAILED, TaskStatus.RUNNING)

    with pytest.raises(ValueError, match="invalid task status transition"):
        validate_task_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)
