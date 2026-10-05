import asyncio

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus
import scriptorium.schemas
from scriptorium.schemas import JudgedScientificReviewOutput, ScientificReviewOutput
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository, review_finding, submit


def _claim_check(*, assessment="supported", finding_indices=None, evidence=None):
    return {
        "claim": "The manuscript reports a clear result.",
        "evidence": evidence or [{"source_path": "manuscript.pdf", "page": 1}],
        "critical_question": "Does the reported result support the conclusion?",
        "countercheck": "Checked the manuscript and its reported result.",
        "claim_anchor": {"source_path": "manuscript.pdf", "page": 1},
        "stated_scope": "As stated in the manuscript.",
        "check_type": "design_and_analysis",
        "question_answer": {"supported": "yes", "unresolved": "not_checkable"}.get(assessment, "no"),
        "exceptions": [],
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
        assert report["review_claim_checks"][0]["submitted_findings"][0]["title"] == finding["title"]
        markdown = service.render_report(run.id, "markdown")
        assert "Scientific claim checks" in markdown
        assert "submitted finding [0]: major — Typo obscures the claim" in markdown
        assert "evidence: `manuscript.pdf:page 1`" in markdown


@pytest.mark.parametrize("completion", ["complete", "partial", "unknown"])
def test_unresolved_external_evidence_is_separate_from_unfinished_frozen_review(tmp_path, completion):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        assert "Mark completion partial or unknown when work remains or cannot be assessed" not in review["prompt"]
        assert "available relevant frozen sources or pages remains unfinished" in review["prompt"]
        assert (
            "do not alone prevent complete after the available relevant material has been assessed" in review["prompt"]
        )
        complete = completion == "complete"
        limitation = "The external raw data needed to resolve the scientific question are unavailable."
        checked = [{"source_path": "main.tex", "start_line": 1, "end_line": 4}]
        if complete:
            checked.append({"source_path": "manuscript.pdf", "page": 1})
        receipt = submit(
            service,
            review,
            {
                "summary": limitation,
                "findings": [],
                "claim_checks": [
                    {
                        **_claim_check(assessment="unresolved", evidence=review_finding(review)["evidence"]),
                        "countercheck": limitation,
                    }
                ],
                "scope": {
                    "completion": completion,
                    "checked": checked,
                    "outstanding": [] if complete else [{"source_path": "manuscript.pdf", "page": 1}],
                    "limitations": [limitation] if complete else [limitation, "The frozen page is still unread."],
                },
            },
        )
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        assert receipt["run_status"] == (RunStatus.AWAITING_DECISION if complete else RunStatus.REVIEWING)
        report = service.render_report(run.id, "json")
        assert report["review_claim_checks"][0]["claim_checks"][0]["assessment"] == "unresolved"
        assert service.database.list_findings(run.id) == []
        assert not service.evaluate_gate(run.id)["passed"]


@pytest.mark.parametrize(("index", "accepted"), [(0.0, True), (0.5, False), ("0", False)])
def test_finding_indices_follow_published_integer_schema(tmp_path, index, accepted):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        receipt = submit(
            service,
            review,
            {
                "summary": "Checked the reported result.",
                "findings": [review_finding(review)],
                "claim_checks": [_claim_check(assessment="finding", finding_indices=[index])],
            },
        )
        assert (receipt["attempt"].status == AttemptStatus.COMPLETED) is accepted
        assert len(service.database.list_findings(run.id)) == int(accepted)


def test_report_indices_refer_to_attempt_submission_order(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        minor = {**review_finding(review), "severity": "minor", "title": "Minor concern"}
        blocker = {**review_finding(review), "severity": "blocker", "title": "Blocking concern"}
        receipt = submit(
            service,
            review,
            {
                "summary": "Checked two concerns.",
                "findings": [minor, blocker],
                "claim_checks": [_claim_check(assessment="finding", finding_indices=[0, 1])],
            },
        )
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        report = service.render_report(run.id, "json")
        assert [item["finding"]["title"] for item in report["findings"]] == ["Blocking concern", "Minor concern"]
        submitted = report["review_claim_checks"][0]["submitted_findings"]
        assert [item["title"] for item in submitted] == ["Minor concern", "Blocking concern"]
        markdown = service.render_report(run.id, "markdown")
        assert "submitted finding [0]: minor — Minor concern" in markdown
        assert "submitted finding [1]: blocker — Blocking concern" in markdown


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


def test_claim_anchor_quote_is_validated_like_evidence(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        anchor = {**review_finding(review)["evidence"][0], "quoted_text": "This text is not in the manuscript."}
        receipt = submit(
            service,
            review,
            {
                "summary": "Checked the result.",
                "findings": [],
                "claim_checks": [{**_claim_check(), "claim_anchor": anchor}],
            },
        )
        assert receipt["attempt"].status == AttemptStatus.FAILED
        assert any("/claim_checks/0/claim_anchor" in str(issue) for issue in receipt["validation_report"]["issues"])


def test_report_renders_claim_judgments_and_recomputation(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        check = {
            **_claim_check(assessment="unresolved"),
            "claim_anchor": review_finding(review)["evidence"][0],
            "stated_scope": "Every reported condition.",
            "check_type": "recomputation",
            "question_answer": "partly",
            "exceptions": ["The second condition is not reported."],
            "recomputation": {
                "inputs": ["a = 2 (line 3)", "b = 3 (line 3)"],
                "calculation": "a + b",
                "result": "5",
                "reported": "5",
                "outcome": "matches",
            },
        }
        receipt = submit(service, review, {"summary": "Checked the sum.", "findings": [], "claim_checks": [check]})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        markdown = service.render_report(run.id, "markdown")
        assert "claim at `main.tex:3-3`; stated scope: Every reported condition." in markdown
        assert "recomputation; answer: partly" in markdown
        assert "exception: The second condition is not reported." in markdown
        assert "recomputation matches: a + b = 5; reported 5; inputs: a = 2 (line 3); b = 3 (line 3)" in markdown


def test_runs_frozen_before_claim_judgments_keep_their_claim_check_shape(tmp_path, monkeypatch):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        with monkeypatch.context() as patch:
            patch.setitem(scriptorium.schemas.SCHEMA_MODELS, "scientific_review", ScientificReviewOutput)
            run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        assert "JudgedClaimCheck" not in review["schema"]["$defs"]
        legacy = {
            key: value
            for key, value in _claim_check().items()
            if key not in {"claim_anchor", "stated_scope", "check_type", "question_answer", "exceptions"}
        }
        receipt = submit(service, review, {"summary": "Checked the result.", "findings": [], "claim_checks": [legacy]})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        markdown = service.render_report(run.id, "markdown")
        assert "Scientific claim checks" in markdown
        assert "stated scope" not in markdown


def test_claim_inventory_anchor_is_validated_and_rendered(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        assert "claim_inventory" in review["schema"]["required"]
        assert "claim_inventory" in review["prompt"]
        anchor = review_finding(review)["evidence"][0]
        headline = {
            "claim": "The result is clear.",
            "claim_anchor": anchor,
            "prominence": "headline",
            "check_indices": [0],
        }
        unchecked = {
            "claim": "The result generalizes.",
            "claim_anchor": {"source_path": "manuscript.pdf", "page": 1},
            "prominence": "supporting",
            "check_indices": [],
            "not_checked_reason": "The general case is outside the frozen bundle.",
        }
        output = {"summary": "Checked the result.", "findings": [], "claim_checks": [_claim_check()]}
        bad = {**headline, "claim_anchor": {**anchor, "quoted_text": "This text is not in the manuscript."}}
        rejected = submit(service, review, {**output, "claim_inventory": [bad, unchecked]})
        assert rejected["attempt"].status == AttemptStatus.FAILED
        assert any("/claim_inventory/0/claim_anchor" in str(issue) for issue in rejected["validation_report"]["issues"])
        asyncio.run(service.retry_task(run.id, review["task"].id))
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="second")
        receipt = submit(service, review, {**output, "claim_inventory": [headline, unchecked]})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        report = service.render_report(run.id, "json")
        assert (
            report["review_claim_checks"][0]["claim_inventory"][1]["not_checked_reason"]
            == unchecked["not_checked_reason"]
        )
        markdown = service.render_report(run.id, "markdown")
        assert "inventoried headline claim at `main.tex:3-3`: The result is clear. — claim checks [0]" in markdown
        assert (
            "inventoried supporting claim at `manuscript.pdf:page 1`: The result generalizes. — not checked: "
            "The general case is outside the frozen bundle." in markdown
        )
        assert "  - [0] The manuscript reports a clear result. — supported" in markdown


def test_complete_review_must_check_every_inventoried_headline_claim(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        page = {"source_path": "manuscript.pdf", "page": 1}
        inventory = [
            {"claim": "The result is clear.", "claim_anchor": page, "prominence": "headline", "check_indices": [0]},
            {
                "claim": "The result generalizes.",
                "claim_anchor": page,
                "prominence": "headline",
                "check_indices": [],
                "not_checked_reason": "Not reached yet.",
            },
        ]
        receipt = submit(
            service,
            review,
            {
                "summary": "Checked one claim.",
                "findings": [],
                "claim_checks": [_claim_check()],
                "claim_inventory": inventory,
            },
        )
        assert receipt["attempt"].status == AttemptStatus.FAILED
        assert "must check every inventoried headline claim" in str(receipt["validation_report"]["issues"])


def test_runs_frozen_before_the_claim_inventory_accept_judged_checks_alone(tmp_path, monkeypatch):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        with monkeypatch.context() as patch:
            patch.setitem(scriptorium.schemas.SCHEMA_MODELS, "scientific_review", JudgedScientificReviewOutput)
            run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        assert "claim_inventory" not in review["schema"]["properties"]
        output = {"summary": "Checked the result.", "findings": [], "claim_checks": [_claim_check()]}
        receipt = submit(service, review, output)
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        assert "claim_inventory" not in service.render_report(run.id, "json")["review_claim_checks"][0]


def test_continuation_preserves_prior_claim_checks_and_accepts_new_checks(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        finding = review_finding(first)
        partial = {
            "summary": "Checked one claim; a page remains.",
            "findings": [finding],
            "claim_checks": [_claim_check(assessment="finding", finding_indices=[0])],
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
        assert '"claim_inventory":[{"check_indices":[0]' in second["prompt"]
        assert "except when a newly assessed claim check must link to an existing concern" in second["prompt"]
        assert second["attempt"].id != first["attempt"].id
        assert (
            submit(
                service,
                second,
                {
                    "summary": "Checked the rendered result.",
                    "findings": [{**finding, "explanation": "The page confirms the same typo."}],
                    "claim_checks": [_claim_check(assessment="finding", finding_indices=[0])],
                },
            )["run_status"]
            == RunStatus.AWAITING_DECISION
        )
        report = service.render_report(run.id, "json")
        assert len(report["review_claim_checks"]) == 2
        assert len(report["findings"]) == 1
        assert all(item["submitted_findings"][0]["title"] == finding["title"] for item in report["review_claim_checks"])


def test_continuation_must_keep_listing_claims_the_accepted_inventory_left_unchecked(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        page = {"source_path": "manuscript.pdf", "page": 1}
        assert "empty check_indices list with a not_checked_reason" in first["prompt"]
        assert "prior inventory left unchecked, one entry per claim" in first["prompt"]
        checked = {
            "claim": "The result is clear.",
            "claim_anchor": page,
            "prominence": "headline",
            "check_indices": [0],
        }
        open_headline = {
            "claim": "The result generalizes.",
            "claim_anchor": review_finding(first)["evidence"][0],
            "prominence": "headline",
            "check_indices": [],
            "not_checked_reason": "Not reached yet.",
        }
        second_open = {**open_headline, "claim": "The result scales."}
        partial = {
            "summary": "Checked one headline claim.",
            "findings": [],
            "claim_checks": [_claim_check()],
            "claim_inventory": [checked, open_headline, second_open],
            "scope": {
                "completion": "partial",
                "checked": [{"source_path": "main.tex", "start_line": 1, "end_line": 4}],
                "outstanding": [{"source_path": "manuscript.pdf", "page": 1}],
                "limitations": ["The general claims remain unchecked."],
            },
        }
        assert submit(service, first, partial)["attempt"].status == AttemptStatus.COMPLETED
        asyncio.run(service.continue_review(run.id, first["task"].id))
        second = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        complete = {"summary": "Checked the page.", "findings": [], "claim_checks": [_claim_check()]}
        resumed = {**open_headline, "check_indices": [0]}
        del resumed["not_checked_reason"]
        # One entry on the shared anchor carries forward only one of the two open claims.
        dropped = submit(service, second, {**complete, "claim_inventory": [resumed]})
        assert dropped["attempt"].status == AttemptStatus.FAILED
        issues = dropped["validation_report"]["issues"]
        assert [issue["code"] for issue in issues] == ["claim_inventory.unchecked_claim_dropped"]
        assert issues[0]["expected"]["claim"] == "The result scales."
        asyncio.run(service.retry_task(run.id, second["task"].id))
        third = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="third")
        demoted = {**second_open, "prominence": "supporting"}
        receipt = submit(service, third, {**complete, "claim_inventory": [resumed, demoted]})
        assert receipt["attempt"].status == AttemptStatus.FAILED
        asyncio.run(service.retry_task(run.id, second["task"].id))
        fourth = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="fourth")
        both = [resumed, {**resumed, "claim": "The result scales.", "check_indices": [1]}]
        receipt = submit(
            service, fourth, {**complete, "claim_checks": [_claim_check(), _claim_check()], "claim_inventory": both}
        )
        assert receipt["attempt"].status == AttemptStatus.COMPLETED


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
