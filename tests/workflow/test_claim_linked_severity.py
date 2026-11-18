import asyncio
import json

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository, review_finding, submit

_PAGE = {"source_path": "manuscript.pdf", "page": 1}


def _check(claim_index=0, finding_indices=()):
    return {
        "claim_index": claim_index,
        "evidence": [_PAGE],
        "critical_question": "Does the reported result support the conclusion?",
        "countercheck": "Checked the frozen manuscript page.",
        "stated_scope": "As stated in the manuscript.",
        "check_type": "design_and_analysis",
        "question_answer": "no" if finding_indices else "yes",
        "exceptions": [],
        "assessment": "finding" if finding_indices else "supported",
        "finding_indices": list(finding_indices),
    }


def _claim(claim="The result is clear.", reason=None):
    entry = {"claim": claim, "claim_anchor": _PAGE, "prominence": "headline"}
    return entry if reason is None else {**entry, "not_checked_reason": reason}


def _verdict(recommendation):
    return {"recommendation": recommendation, "decisive_questions": ["Is the main result sentence readable?"]}


def _output(findings, recommendation, *, checks=None, inventory=None):
    return {
        "summary": "Checked the reported result.",
        "findings": findings,
        "limitations": [],
        "claim_checks": checks if checks is not None else [_check(finding_indices=range(len(findings)))],
        "claim_inventory": inventory if inventory is not None else [_claim()],
        "verdict": _verdict(recommendation),
    }


def _issues(receipt):
    return receipt["validation_report"]["issues"]


def test_new_substantive_review_requires_indexed_checks_and_reports_the_verdict_first(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        assert "verdict" in review["schema"]["required"]
        assert "Decide the verdict before listing findings" in review["prompt"]
        finding = review_finding(review)
        restated = _output([finding], "major_revision")
        restated["claim_checks"][0] |= {"claim": "The result is clear.", "claim_anchor": _PAGE}
        # Submitted directly: the shared helper would rewrite a restated check into the indexed shape.
        rejected = asyncio.run(service.submit_task(review["attempt"].id, review["input_digest"], json.dumps(restated)))
        assert rejected["attempt"].status == AttemptStatus.FAILED
        assert "Extra inputs" in str(_issues(rejected))
        asyncio.run(service.retry_task(run.id, review["task"].id))
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="second")
        receipt = submit(service, review, _output([finding], "major_revision"))
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        entry = service.render_report(run.id, "json")["review_claim_checks"][0]
        assert entry["verdict"] == _verdict("major_revision")
        markdown = service.render_report(run.id, "markdown")
        verdict = "  - verdict: major_revision; decisive questions: Is the main result sentence readable?"
        assert markdown.index(verdict) < markdown.index("submitted finding [0]")
        assert "inventoried headline claim at `manuscript.pdf:page 1`: The result is clear. — claim checks [0]" in (
            markdown
        )
        assert "  - [0] The result is clear. — finding" in markdown
        assert "    - claim at `manuscript.pdf:page 1`; stated scope: As stated in the manuscript." in markdown


@pytest.mark.parametrize(
    ("severity", "recommendation"),
    [
        ("moderate", "major_revision"),
        ("minor", "reject"),
        (None, "reject"),
        ("major", "accept"),
        ("major", "minor_revision"),
    ],
)
def test_verdict_must_agree_with_the_review_findings(tmp_path, severity, recommendation):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        findings = [] if severity is None else [{**review_finding(review), "severity": severity}]
        receipt = submit(service, review, _output(findings, recommendation))
        assert receipt["attempt"].status == AttemptStatus.FAILED
        issues = _issues(receipt)
        assert recommendation in str(issues)
        if recommendation in {"reject", "major_revision"}:
            assert [(issue["code"], issue["path"]) for issue in issues] == [
                ("verdict.inconsistent", "/verdict/recommendation")
            ]
        assert service.database.list_findings(run.id) == []


def test_blocker_finding_needs_a_check_of_a_headline_claim(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        blocker = {**review_finding(review), "severity": "blocker"}
        supporting = {**_claim(), "prominence": "supporting"}
        receipt = submit(service, review, _output([blocker], "reject", inventory=[supporting]))
        assert receipt["attempt"].status == AttemptStatus.FAILED
        assert "a blocker finding needs a headline claim" in str(_issues(receipt))


@pytest.mark.parametrize("role", ["copyedit", "consistency", "figure_review"])
def test_other_review_roles_must_name_the_affected_claim_at_moderate_or_above(tmp_path, role):
    repo = make_repository(tmp_path, roles=(role,))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, role)
        finding = {key: value for key, value in review_finding(review).items() if key != "affected_claim"}
        rejected = submit(service, review, {"summary": "Checked.", "findings": [{**finding, "severity": "moderate"}]})
        assert rejected["attempt"].status == AttemptStatus.FAILED
        assert "must name its affected_claim" in str(_issues(rejected))
        asyncio.run(service.retry_task(run.id, review["task"].id))
        review = claim(service, run.id, role, session="second")
        minor = {**finding, "severity": "minor"}
        receipt = submit(service, review, {"summary": "Checked.", "findings": [minor]})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED


def test_verdict_counts_its_own_findings_deduplicated_into_another_task(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review", "copyedit"))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        copyedit = claim(service, run.id, AgentRole.COPYEDIT)
        shared = review_finding(copyedit)
        receipt = submit(service, copyedit, {"summary": "Found a typo.", "findings": [shared]})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        same = {key: value for key, value in shared.items() if key != "affected_claim"}
        # Accepting would contradict this submission's major finding, whichever task owns its stored row.
        accepted = submit(service, first, _output([same], "accept"))
        assert accepted["attempt"].status == AttemptStatus.FAILED
        asyncio.run(service.retry_task(run.id, first["task"].id))
        second = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="second")
        receipt = submit(service, second, _output([same], "major_revision"))
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        assert receipt["run_status"] == RunStatus.AWAITING_DECISION
        # The identical finding keeps the copyedit task's row, yet it is part of this review's accepted output.
        assert [finding.task_id for finding in service.database.list_findings(run.id)] == [copyedit["task"].id]


def test_affected_claim_is_stored_and_reported(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.COPYEDIT)
        finding = review_finding(first)
        minor = {**finding, "severity": "minor", "title": "Minor wording"}
        del minor["affected_claim"]
        output = {"summary": "Checked the text.", "findings": [finding, minor]}
        assert submit(service, first, output)["attempt"].status == AttemptStatus.COMPLETED
        stored = {item.title: item for item in service.database.list_findings(run.id)}
        assert stored[finding["title"]].affected_claim == finding["affected_claim"]
        assert stored["Minor wording"].affected_claim is None
        shown = service.get_finding(stored[finding["title"]].id)["finding"]
        assert shown.affected_claim == finding["affected_claim"]
        report = service.render_report(run.id, "json")
        assert {item["finding"]["title"]: item["finding"]["affected_claim"] for item in report["findings"]} == {
            finding["title"]: finding["affected_claim"],
            "Minor wording": None,
        }
        assert f"  - affected claim: {finding['affected_claim']}" in service.render_report(run.id, "markdown")


def test_a_duplicate_finding_fills_a_missing_affected_claim_once(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit", "consistency"))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.COPYEDIT)
        minor = {**review_finding(first), "severity": "minor", "title": "Minor wording"}
        del minor["affected_claim"]
        submit(service, first, {"summary": "Checked the text.", "findings": [minor]})
        (original,) = service.database.list_findings(run.id)
        assert original.affected_claim is None
        second = claim(service, run.id, AgentRole.CONSISTENCY)
        named = {**minor, "affected_claim": "The headline accuracy claim."}
        submit(service, second, {"summary": "Checked the page.", "findings": [named]})

        (filled,) = service.database.list_findings(run.id)
        assert (filled.id, filled.fingerprint) == (original.id, original.fingerprint)
        assert filled.affected_claim == "The headline accuracy claim."
        events = [event.event_type for event in service.database.list_events(run.id)]
        assert events.count("finding.created") == 1
        assert events.count("finding.duplicate") == 1

        assert (
            service.database.fill_finding_affected_claim(filled.id, "A later claim.").affected_claim
            == "The headline accuracy claim."
        )
