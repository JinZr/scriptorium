import asyncio
import json

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus
from scriptorium.service import ScriptoriumService

from ._support import (
    MANUSCRIPT,
    PdfBuildingManuscriptManager,
    approve_plan,
    claim,
    complete_reviews,
    export_writing,
    make_repository,
    prepare_patch,
    prepare_verification,
    review_finding,
    submit,
)


def test_pdf_page_evidence_anchors_the_frozen_rendered_page(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        finding = review_finding(review)
        finding["evidence"] = [{"source_path": "manuscript.pdf", "page": 1}]
        receipt = submit(service, review, {"summary": "The rendered page was inspected.", "findings": [finding]})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        assert service.list_findings(run.id)[0].evidence[0] == {"source_path": "manuscript.pdf", "page": 1}


def test_pdf_page_evidence_rejects_a_quote_field(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        finding = review_finding(review)
        finding["evidence"] = [{"source_path": "manuscript.pdf", "page": 1, "quoted_text": "not a page quote"}]
        receipt = submit(service, review, {"summary": "Inspected the page.", "findings": [finding]})
        assert receipt["attempt"].status == AttemptStatus.FAILED
        assert service.list_findings(run.id) == []
        assert receipt["validation_report"]["issues"]


def test_full_workflow_preserves_worktree_until_approved_patch_is_applied(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        patch, finding_id = prepare_verification(service, run.id)
        assert (repo / "main.tex").read_text() == MANUSCRIPT
        assert service.store.get_run(run.id).status == RunStatus.VERIFYING
        assert not service.evaluate_gate(run.id)["passed"]

        verifier = claim(service, run.id, AgentRole.VERIFICATION, session="independent-session")
        receipt = submit(
            service,
            verifier,
            {
                "verdict": "pass",
                "summary": "The corrected sentence resolves the finding.",
                "resolved_finding_ids": [finding_id],
                "issues": [],
            },
        )
        assert receipt["run_status"] == RunStatus.READY_TO_APPLY
        assert (repo / "main.tex").read_text() == MANUSCRIPT
        assert not service.evaluate_gate(run.id)["passed"]
        service.apply_patch(patch.id)
        assert "The result is clear." in (repo / "main.tex").read_text()
        assert service.evaluate_gate(run.id)["passed"]


def test_task_list_handoffs_patch_approval_and_replays_verification(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        patch, finding_id = prepare_patch(service, run.id)
        assert service.list_tasks(run.id)["next_actions"] == [
            {"command": "patch show", "patch_id": patch.id, "requires_human_decision": True}
        ]
        service.decide_patch(patch.id, "approve", "Verify the exact edit.")
        assert service.list_tasks(run.id)["next_actions"] == [{"command": "run resume", "run_id": run.id}]
        asyncio.run(service.resume_run(run.id))
        verifier = claim(service, run.id, AgentRole.VERIFICATION, session="independent-session")
        service.armarius.submit_task(
            verifier["attempt"].id,
            verifier["input_digest"],
            json.dumps(
                {
                    "verdict": "pass",
                    "summary": "The corrected sentence resolves the finding.",
                    "resolved_finding_ids": [finding_id],
                    "issues": [],
                }
            ),
        )
        assert service.store.get_run(run.id).status == RunStatus.VERIFYING
        assert service.list_tasks(run.id)["next_actions"] == [{"command": "run resume", "run_id": run.id}]
        asyncio.run(service.resume_run(run.id))
        assert service.store.get_run(run.id).status == RunStatus.READY_TO_APPLY
        assert service.list_tasks(run.id)["next_actions"] == [
            {"command": "patch apply", "patch_id": patch.id, "requires_human_decision": True}
        ]


def test_run_resume_replays_accepted_writing_into_a_patch(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        complete_reviews(service, run.id)
        finding_id = service.list_findings(run.id)[0].id
        service.decide_finding(finding_id, "confirm", "Correct the typo.")
        asyncio.run(service.resume_run(run.id))
        assert service.store.get_run(run.id).status == RunStatus.PLANNING
        approve_plan(service, run.id)
        writing, export = export_writing(service, run.id)
        (export / "task" / "passages" / "1.tex").write_text("The result is clear.", encoding="utf-8")
        output = service.pack_writing_output(writing["attempt"].id, export)
        # Accepted without advancing the run, as when a process stops after accepting an output.
        service.armarius.submit_task(writing["attempt"].id, writing["input_digest"], output)
        assert service.store.get_run(run.id).status == RunStatus.WRITING
        assert service.list_tasks(run.id)["next_actions"] == [{"command": "run resume", "run_id": run.id}]
        asyncio.run(service.resume_run(run.id))
        assert service.store.get_run(run.id).status == RunStatus.AWAITING_PATCH_APPROVAL
        patch = service.store.list_patches(run.id)[-1]
        assert patch.edits[0]["after"] == "The result is clear.\n"
        assert patch.edits[0]["finding_ids"] == [finding_id]
