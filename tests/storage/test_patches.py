import pytest

from scriptorium.domain import Artifact, Patch, PatchStatus, RunStatus, Verification, VerificationResult
from scriptorium.storage import ConflictError, RunStore

from ._factories import approve_plan, make_run


def test_patch_verification_and_artifact_metadata(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        plan = approve_plan(store, run)
        artifact = store.add_artifact(
            Artifact(
                digest="e" * 64,
                relative_path="sha256/ee/" + "e" * 62,
                size=123,
                media_type="text/x-diff",
            )
        )
        patch = store.create_patch(
            Patch(
                run_id=run.id,
                base_commit=run.commit_sha,
                diff_digest=artifact.digest,
                summary="Clarify one sentence.",
                edits=({"path": "main.tex", "before": "old", "after": "new"},),
                plan_id=plan.id,
            )
        )
        store.decide_patch(patch.id, "approve", "Reviewed locally.")
        verification = store.create_verification(
            Verification(
                patch_id=patch.id,
                result=VerificationResult.PASS,
                summary="No regression found.",
            )
        )

        assert store.get_artifact(artifact.digest) == artifact
        assert store.get_patch(patch.id).plan_id == plan.id
        assert store.get_patch(patch.id).status == PatchStatus.VERIFIED
        assert store.list_verifications(patch.id) == [verification]
        applied = store.update_patch(patch.id, PatchStatus.APPLIED)
        assert applied.applied_at is not None


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
        decision = store.decide_patch(patch.id, "reject", "Use more precise wording.")
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
                plan_id=first_plan.id,
            )
        )
        store.decide_patch(second_patch.id, "reject", "Return to the earlier wording.")
        store.update_run(run.id, RunStatus.PLANNING)
        second_plan = approve_plan(store, run, "8" * 64)

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

        with pytest.raises(ConflictError, match="cannot be reproposed in status proposed"):
            store.repropose_patch(
                patch.id,
                plan_id=second_plan.id,
                summary="Third wording.",
                edits=(),
                build_succeeded=True,
            )
