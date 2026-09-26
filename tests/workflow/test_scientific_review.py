import asyncio

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository, review_finding, submit


def _claim_check(*, assessment="supported", finding_indices=None, evidence=None):
    return {
        "claim": "The manuscript reports a clear result.",
        "evidence": evidence or [{"source_path": "manuscript.pdf", "page": 1}],
        "critical_question": "Does the reported result support the conclusion?",
        "countercheck": "Checked the manuscript and its reported result.",
        "assessment": assessment,
        "finding_indices": finding_indices or [],
    }


def test_substantive_review_records_anchored_claim_checks_and_links_findings(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        assert "claim_checks" in review["schema"]["required"]
        assert "critical question" in review["prompt"]
        finding = review_finding(review)
        receipt = submit(
            service,
            review,
            {
                "summary": "Checked the reported result.",
                "findings": [finding],
                "claim_checks": [_claim_check(assessment="finding", finding_indices=[0])],
            },
        )
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        assert receipt["run_status"] == RunStatus.AWAITING_DECISION
        assert len(service.database.list_findings(run.id)) == 1
        report = service.render_report(run.id, "json")
        assert report["review_claim_checks"][0]["claim_checks"][0]["finding_indices"] == [0]
        markdown = service.render_report(run.id, "markdown")
        assert "Scientific claim checks" in markdown
        assert "evidence: `manuscript.pdf:page 1`" in markdown


@pytest.mark.parametrize("invalid", ["empty", "unlinked", "bad_anchor"])
def test_scientific_review_rejects_missing_or_invalid_checks_without_findings(tmp_path, invalid):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        finding = review_finding(review) if invalid == "unlinked" else None
        checks = [] if invalid == "empty" else [_claim_check()]
        if invalid == "bad_anchor":
            source = next(item for item in review["source_map"]["sources"] if item["source_path"] == "main.tex")
            checks[0]["evidence"] = [
                {
                    "source_path": "main.tex",
                    "start_line": 3,
                    "end_line": 3,
                    "source_digest": source["source_digest"],
                    "quoted_text": "This text is not in the manuscript.",
                }
            ]
        receipt = submit(
            service,
            review,
            {
                "summary": "Checked the reported result.",
                "findings": [finding] if finding else [],
                "claim_checks": checks,
            },
        )
        assert receipt["attempt"].status == AttemptStatus.FAILED
        assert receipt["validation_report"]["issues"]
        assert service.database.list_findings(run.id) == []
        assert not service.evaluate_gate(run.id)["passed"]


def test_continuation_preserves_prior_claim_checks_and_accepts_new_checks(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        partial = {
            "summary": "Checked one claim; a page remains.",
            "findings": [],
            "claim_checks": [_claim_check()],
            "scope": {
                "completion": "partial",
                "checked": [{"source_path": "main.tex", "start_line": 1, "end_line": 4}],
                "outstanding": [{"source_path": "manuscript.pdf", "page": 1}],
                "limitations": ["The rendered page remains unchecked."],
            },
        }
        assert submit(service, first, partial)["attempt"].status == AttemptStatus.COMPLETED
        asyncio.run(service.continue_review(run.id, first["task"].id))
        second = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        assert "claim_checks" in second["prompt"]
        assert second["attempt"].id != first["attempt"].id
        assert (
            submit(
                service,
                second,
                {
                    "summary": "Checked the rendered result.",
                    "findings": [],
                    "claim_checks": [_claim_check()],
                },
            )["run_status"]
            == RunStatus.AWAITING_DECISION
        )
        report = service.render_report(run.id, "json")
        assert len(report["review_claim_checks"]) == 2


def test_other_review_roles_keep_the_scoped_review_schema(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.COPYEDIT)
        assert "claim_checks" not in review["schema"]["properties"]
        assert (
            submit(service, review, {"summary": "Checked language.", "findings": []})["attempt"].status
            == AttemptStatus.COMPLETED
        )
