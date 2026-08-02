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
    assert ROLE_CATALOG[AgentRole.VISUAL_TRANSCRIPTION].display_name == "Visual Transcriber"
    assert ROLE_CATALOG[AgentRole.SUBSTANTIVE_REVIEW].display_name == "Scholiast"
    assert ROLE_CATALOG[AgentRole.COPYEDIT].display_name == "Corrector"
    assert ROLE_CATALOG[AgentRole.CONSISTENCY].display_name == "Collator"
    assert ROLE_CATALOG[AgentRole.FIGURE_REVIEW].display_name == "Figure Reviewer"
    assert ROLE_CATALOG[AgentRole.REVISION].display_name == "Scribe"
    assert ROLE_CATALOG[AgentRole.VERIFICATION].display_name == "Verifier"


def test_canonical_json_digest_is_order_independent() -> None:
    assert digest_json({"b": 2, "a": [1, 3]}) == digest_json({"a": [1, 3], "b": 2})
    assert digest_json({"a": 1}) != digest_json({"a": 2})


def test_run_status_transitions_are_explicit() -> None:
    validate_run_transition(RunStatus.PREPARING, RunStatus.REVIEWING)
    validate_run_transition(RunStatus.REVIEWING, RunStatus.WAITING_BUDGET)
    validate_run_transition(RunStatus.WAITING_BUDGET, RunStatus.REVIEWING)
    validate_run_transition(RunStatus.READY_TO_APPLY, RunStatus.COMPLETED)

    with pytest.raises(ValueError, match="invalid run status transition"):
        validate_run_transition(RunStatus.PREPARING, RunStatus.COMPLETED)


def test_task_can_retry_only_after_failure_or_interruption() -> None:
    validate_task_transition(TaskStatus.PENDING, TaskStatus.RUNNING)
    validate_task_transition(TaskStatus.INTERRUPTED, TaskStatus.RUNNING)
    validate_task_transition(TaskStatus.FAILED, TaskStatus.RUNNING)

    with pytest.raises(ValueError, match="invalid task status transition"):
        validate_task_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)
