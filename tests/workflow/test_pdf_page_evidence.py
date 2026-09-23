import asyncio

import pytest

from scriptorium.domain import AgentRole, AttemptStatus
from scriptorium.errors import InfrastructureError
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository, review_finding, submit


def test_pdf_page_anchor_uses_frozen_page_identity_without_a_quote(tmp_path):
    repo = make_repository(tmp_path, roles=("figure_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.FIGURE_REVIEW)
        page = service.page_task(review["attempt"].id, 1)
        assert page["page"] == 1
        assert page["path"].endswith("page-0001.png")
        assert len(page["digest"]) == 64
        finding = review_finding(review)
        finding["evidence"] = [{"source_path": "manuscript.pdf", "page": 1}]
        receipt = submit(service, review, {"summary": "Inspected the rendered page.", "findings": [finding]})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        assert service.list_findings(run.id)[0].evidence[0] == finding["evidence"][0]
        assert any(event.event_type == "tool.page" for event in service.database.list_events(run.id))


def test_corrupt_frozen_page_cannot_be_used_as_evidence(tmp_path):
    repo = make_repository(tmp_path, roles=("figure_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.FIGURE_REVIEW)
        page = repo / ".scriptorium" / "runs" / run.id / "bundle" / "pages" / "page-0001.png"
        page.write_bytes(b"corrupt")
        with pytest.raises(InfrastructureError, match="bundle|page"):
            service.page_task(review["attempt"].id, 1)
