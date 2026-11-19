import json

import pytest

from scriptorium.domain import Plan, RunStatus
from scriptorium.storage import ConflictError, NotFoundError, RunStore, StorageError

from ._factories import approve_plan, make_run


def test_an_approved_plan_is_recorded_with_its_decision_and_moves_the_run_to_writing(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        plan = approve_plan(store, run)
        assert store.get_run(run.id).status == RunStatus.WRITING
        assert store.list_plans(run.id) == [plan]
        (decision,) = store.list_decisions("plan", plan.id)
        assert (decision.target_type, decision.decision, decision.reason) == (
            "plan",
            "approve",
            "The author approved the plan.",
        )
        events = [event.event_type for event in store.list_events(run.id)]
        assert events[-2:] == ["plan.decided", "run.status_changed"]

    document = json.loads((tmp_path / "runs" / run.id / "state.json").read_bytes())
    assert [item["id"] for item in document["plans"]] == [plan.id]
    with RunStore(tmp_path) as reopened:
        assert reopened.get_plan(plan.id) == plan
        assert reopened.list_plans(run.id) == [plan]


def test_a_plan_is_approved_only_while_planning_and_with_recorded_files(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        unrecorded = Plan(run_id=run.id, digest="9" * 64, plan_artifact_digest="9" * 64, assignments=())
        with pytest.raises(ConflictError, match="while the run is preparing"):
            store.approve_plan(unrecorded, "Approved.")
        for status in (RunStatus.REVIEWING, RunStatus.AWAITING_DECISION, RunStatus.PLANNING):
            store.update_run(run.id, status)
        with pytest.raises(StorageError, match="artifact is not recorded"):
            store.approve_plan(unrecorded, "Approved.")
        with pytest.raises(ValueError, match="reason is required"):
            store.approve_plan(unrecorded, " ")
        assert store.list_plans(run.id) == [] and store.get_run(run.id).status == RunStatus.PLANNING
        with pytest.raises(NotFoundError, match="plan not found"):
            store.get_plan(unrecorded.id)


def test_the_latest_plan_is_listed_last(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        first = approve_plan(store, run)
        store.update_run(run.id, RunStatus.PLANNING)
        second = approve_plan(store, run, "8" * 64)
        assert [item.id for item in store.list_plans(run.id)] == [first.id, second.id]
