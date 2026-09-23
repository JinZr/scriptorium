import asyncio

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus
from scriptorium.service import ScriptoriumService

from ._support import (
    MANUSCRIPT,
    PdfBuildingManuscriptManager,
    claim,
    make_repository,
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
        assert service.database.get_run(run.id).status == RunStatus.VERIFYING
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
