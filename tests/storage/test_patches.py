import pytest

from scriptorium.domain import Artifact, Check, CheckResult, Patch, PatchStatus, RunStatus
from scriptorium.storage import ConflictError, RunStore, StorageError

from ._factories import approve_plan, create_completed_attempt, make_run


def _diff(store, digest):
    return store.add_artifact(
        Artifact(digest=digest, relative_path=f"sha256/{digest[:2]}/{digest[2:]}", size=12, media_type="text/x-diff")
    )


def _check(patch, result, verdict="accept", feedback=None, attempt_id=None):
    return Check(
        patch_id=patch.id,
        result=result,
        summary="Checked against the plan.",
        assignments=(
            {"assignment_id": "results", "writing_attempt_id": attempt_id, "verdict": verdict, "feedback": feedback},
        ),
    )


def test_a_passing_check_awaits_the_author_and_an_approved_patch_is_applied(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        plan = approve_plan(store, run)
        _, attempt = create_completed_attempt(store, run)
        artifact = _diff(store, "e" * 64)
        patch = store.create_patch(
            Patch(
                run_id=run.id,
                base_commit=run.commit_sha,
                diff_digest=artifact.digest,
                summary="Clarify one sentence.",
                edits=({"path": "main.tex", "before": "old", "after": "new"},),
                plan_id=plan.id,
                sources=({"assignment_id": "results", "attempt_id": attempt.id},),
                facts_digest=artifact.digest,
            )
        )
        with pytest.raises(ConflictError, match="cannot be decided in status proposed"):
            store.decide_patch(patch.id, "approve", "Too early.")
        with pytest.raises(ConflictError, match="while the run is writing"):
            store.record_check(_check(patch, CheckResult.PASS, attempt_id=attempt.id))
        store.update_run(run.id, RunStatus.CHECKING)
        check = store.record_check(_check(patch, CheckResult.PASS, attempt_id=attempt.id))

        assert store.get_patch(patch.id).status == PatchStatus.CHECKED
        assert store.get_run(run.id).status == RunStatus.AWAITING_PATCH_APPROVAL
        assert store.list_checks(run.id) == store.list_checks(run.id, patch.id) == [check]
        assert store.list_checks(run.id, "patch_other") == []
        assert [event.event_type for event in store.list_events(run.id)[-3:]] == [
            "check.created",
            "patch.status_changed",
            "run.status_changed",
        ]
        with pytest.raises(ConflictError, match="cannot be checked in status checked"):
            store.record_check(_check(patch, CheckResult.PASS))
        with pytest.raises(ValueError, match="invalid patch status transition: checked -> applied"):
            store.update_patch(patch.id, PatchStatus.APPLIED)
        store.decide_patch(patch.id, "reject", "Reconsider.")
        store.decide_patch(patch.id, "approve", "Reviewed locally.")
        assert store.get_patch(patch.id).plan_id == plan.id
        assert store.get_patch(patch.id).sources == ({"assignment_id": "results", "attempt_id": attempt.id},)
        applied = store.update_patch(patch.id, PatchStatus.APPLIED)
        assert applied.applied_at is not None
    with RunStore(tmp_path) as reopened:
        assert reopened.list_checks(run.id) == [check]


def test_a_returning_check_sends_the_run_back_to_writing_and_the_patch_can_be_proposed_again(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        plan = approve_plan(store, run)
        _, attempt = create_completed_attempt(store, run)
        patch = store.create_patch(
            Patch(run.id, run.commit_sha, _diff(store, "f" * 64).digest, "First draft.", (), plan_id=plan.id)
        )
        store.update_run(run.id, RunStatus.CHECKING)
        store.record_check(_check(patch, CheckResult.FAIL, "redo", "Name the benchmark.", attempt.id))
        assert store.get_patch(patch.id).status == PatchStatus.RETURNED
        assert store.get_run(run.id).status == RunStatus.WRITING
        with pytest.raises(ConflictError, match="cannot be decided in status returned"):
            store.decide_patch(patch.id, "approve", "Not checked.")
        with pytest.raises(StorageError, match="attempt is not recorded"):
            store.repropose_patch(
                patch.id,
                plan_id=plan.id,
                summary="Same diff.",
                edits=(),
                build_succeeded=True,
                sources=({"assignment_id": "results", "attempt_id": "attempt_missing"},),
            )
        sources = ({"assignment_id": "results", "attempt_id": attempt.id},)
        reproposed = store.repropose_patch(
            patch.id, plan_id=plan.id, summary="Same diff.", edits=(), build_succeeded=True, sources=sources
        )
        assert reproposed.status == PatchStatus.PROPOSED and reproposed.sources == sources
        event = store.list_events(run.id)[-1]
        assert event.event_type == "patch.reproposed" and event.payload["from"] == "returned"
        events = len(store.list_events(run.id))
        again = store.repropose_patch(
            patch.id, plan_id=plan.id, summary="Same diff.", edits=(), build_succeeded=True, sources=sources
        )
        assert again == reproposed and len(store.list_events(run.id)) == events


def test_new_patch_requires_the_plan_it_was_built_from(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        with pytest.raises(ValueError, match="plan they were built from"):
            store.create_patch(
                Patch(
                    run_id="run_missing",
                    base_commit="a" * 40,
                    diff_digest="d" * 64,
                    summary="Missing provenance",
                    edits=(),
                )
            )


def test_repropose_rejected_patch_updates_lineage_and_preserves_decision(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        first_plan = approve_plan(store, run)
        artifact = store.add_artifact(
            Artifact(
                digest="1" * 64,
                relative_path="sha256/11/" + "1" * 62,
                size=12,
                media_type="text/x-diff",
            )
        )
        patch = store.create_patch(
            Patch(
                run_id=run.id,
                base_commit=run.commit_sha,
                diff_digest=artifact.digest,
                summary="Initial wording.",
                edits=({"path": "main.tex", "after": "first"},),
                plan_id=first_plan.id,
            )
        )
        store.update_run(run.id, RunStatus.CHECKING)
        store.record_check(_check(patch, CheckResult.PASS))
        decision = store.decide_patch(patch.id, "reject", "Use more precise wording.")
        store.update_run(run.id, RunStatus.PLANNING)
        second_plan = approve_plan(store, run, "8" * 64)
        second_artifact = store.add_artifact(
            Artifact(
                digest="2" * 64,
                relative_path="sha256/22/" + "2" * 62,
                size=14,
                media_type="text/x-diff",
            )
        )
        second_patch = store.create_patch(
            Patch(
                run_id=run.id,
                base_commit=run.commit_sha,
                diff_digest=second_artifact.digest,
                summary="Different wording.",
                edits=({"path": "main.tex", "after": "different"},),
                plan_id=second_plan.id,
            )
        )
        store.update_run(run.id, RunStatus.CHECKING)
        store.record_check(_check(second_patch, CheckResult.PASS))
        store.decide_patch(second_patch.id, "reject", "Return to the earlier wording.")
        store.update_run(run.id, RunStatus.PLANNING)
        approve_plan(store, run, "9" * 64)

        reproposed = store.repropose_patch(
            patch.id,
            plan_id=second_plan.id,
            summary="Revised wording.",
            edits=({"path": "main.tex", "after": "second"},),
            build_succeeded=True,
        )

        assert reproposed.id == patch.id
        assert reproposed.status == PatchStatus.PROPOSED
        assert reproposed.plan_id == second_plan.id
        assert reproposed.summary == "Revised wording."
        assert reproposed.edits == ({"path": "main.tex", "after": "second"},)
        assert store.list_decisions("patch", patch.id) == [decision]
        assert store.list_patches(run.id)[-1].id == patch.id
        event = next(event for event in store.list_events(run.id) if event.event_type == "patch.reproposed")
        assert event.payload == {
            "from": "rejected",
            "to": "proposed",
            "previous_plan_id": first_plan.id,
            "plan_id": second_plan.id,
            "diff_digest": artifact.digest,
        }

        store.update_run(run.id, RunStatus.CHECKING)
        store.record_check(_check(reproposed, CheckResult.PASS))
        assert store.get_run(run.id).status == RunStatus.AWAITING_PATCH_APPROVAL
        with pytest.raises(ConflictError, match="cannot be reproposed in status checked"):
            store.repropose_patch(
                patch.id,
                plan_id=second_plan.id,
                summary="Third wording.",
                edits=(),
                build_succeeded=True,
            )
