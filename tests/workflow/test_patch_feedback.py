import asyncio

import pytest

from scriptorium.domain import PatchStatus, RunStatus
from scriptorium.errors import ConfigurationError
from scriptorium.service import ScriptoriumService

from ._support import (
    PdfBuildingManuscriptManager,
    complete_reviews,
    export_writing,
    make_repository,
    submit_writing,
    write_plan,
)


def _rejected_run(service, tmp_path):
    run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
    complete_reviews(service, run.id)
    finding_id = service.list_findings(run.id)[0].id
    service.decide_finding(finding_id, "confirm", "Correct the typo.")
    asyncio.run(service.resume_run(run.id))
    first = write_plan(tmp_path / "plan-1", {"result": (["main.tex:3-3"], [finding_id], "Correct the typo.")})
    asyncio.run(service.approve_plan(run.id, first, "The author approved the first plan."))
    writing, export = export_writing(service, run.id, session="writer-1")
    submit_writing(service, writing, export, {1: "The result is clear."})
    patch = service.store.list_patches(run.id)[-1]
    service.decide_patch(patch.id, "reject", "Use more precise wording.")
    return run, finding_id, first, patch


def test_a_rejected_patch_returns_the_run_to_planning_and_an_unchanged_plan_is_refused(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run, finding_id, first, first_patch = _rejected_run(service, tmp_path)
        assert service.list_tasks(run.id)["next_actions"] == [{"command": "run resume", "run_id": run.id}]
        tasks_before = len(service.store.list_tasks(run.id))
        resumed = asyncio.run(service.resume_run(run.id))
        assert resumed["run"].status == RunStatus.PLANNING
        # No task is prepared: the lead reads the reason and changes the plan with the author.
        assert len(service.store.list_tasks(run.id)) == tasks_before
        assert service.list_tasks(run.id)["next_actions"] == [
            {"command": "patch show", "patch_id": first_patch.id},
            {"command": "plan approve", "run_id": run.id, "requires_human_decision": True},
        ]
        assert service.get_patch(first_patch.id)["decisions"][-1].reason == "Use more precise wording."

        check = service.check_plan(run.id, first)
        assert not check["valid"]
        assert [issue["code"] for issue in check["issues"]] == ["plan.unchanged_after_rejection"]
        with pytest.raises(ConfigurationError, match="plan.unchanged_after_rejection"):
            asyncio.run(service.approve_plan(run.id, first, "Approve it again."))
        assert len(service.store.list_plans(run.id)) == 1

        second = write_plan(
            tmp_path / "plan-2",
            {"result": (["main.tex:3-3"], [finding_id], "Correct the typo and state the result precisely.")},
        )
        asyncio.run(service.approve_plan(run.id, second, "The author approved the precise wording."))
        writing, export = export_writing(service, run.id, session="writer-2")
        receipt = submit_writing(service, writing, export, {1: "The result is clear and precise."})
        assert receipt["run_status"] == RunStatus.AWAITING_PATCH_APPROVAL
        patches = service.store.list_patches(run.id)
        assert [patch.id for patch in patches][0] == first_patch.id
        assert patches[-1].id != first_patch.id
        assert patches[-1].plan_id == service.store.list_plans(run.id)[-1].id
        assert "teh clear" in (repo / "main.tex").read_text()


def test_an_identical_diff_from_a_new_plan_reproposes_the_rejected_patch(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run, finding_id, _, first_patch = _rejected_run(service, tmp_path)
        asyncio.run(service.resume_run(run.id))
        second = write_plan(
            tmp_path / "plan-2",
            {"result": (["main.tex:3-3"], [finding_id], "Correct the typo; the author reconsidered.")},
        )
        asyncio.run(service.approve_plan(run.id, second, "The author approved the reconsidered plan."))
        writing, export = export_writing(service, run.id, session="writer-2")
        submit_writing(service, writing, export, {1: "The result is clear."})
        patches = service.store.list_patches(run.id)
        assert [patch.id for patch in patches] == [first_patch.id]
        assert patches[0].status == PatchStatus.PROPOSED
        assert patches[0].plan_id == service.store.list_plans(run.id)[-1].id
