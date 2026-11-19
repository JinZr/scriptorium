import asyncio
import json

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus, TaskStatus
from scriptorium.errors import StateError
from scriptorium.service import ScriptoriumService

from ._support import (
    PdfBuildingManuscriptManager,
    check_output,
    claim,
    claim_check,
    link_claims,
    make_repository,
    prepare_patch,
    review_finding,
    submit,
    submit_check,
)


def _review_output(review, *, findings=None):
    findings = [review_finding(review)] if findings is None else findings
    output = {
        "summary": "Checked the reported result.",
        "findings": findings,
        "limitations": [],
        "claim_checks": [
            {
                "claim": "The reported result is clear.",
                "evidence": [{"source_path": "manuscript.pdf", "page": 1}],
                "critical_question": "Does the reported result support the conclusion?",
                "countercheck": "Checked the frozen manuscript page.",
                "claim_anchor": {"source_path": "manuscript.pdf", "page": 1},
                "stated_scope": "As stated in the manuscript.",
                "check_type": "design_and_analysis",
                "question_answer": "no" if findings else "yes",
                "exceptions": [],
                "assessment": "finding" if findings else "supported",
                "finding_indices": list(range(len(findings))),
            }
        ],
    }
    return link_claims(output)


def _state(service, run_id, attempt_id):
    return (
        service.store.get_attempt(attempt_id),
        [task.status for task in service.store.list_tasks(run_id)],
        service.store.get_run(run_id).status,
        service.store.list_findings(run_id),
    )


def test_check_reports_issues_without_recording_or_ending_the_attempt(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        attempt_id = review["attempt"].id
        artifacts_before = sorted(path.name for path in service.artifacts.root.rglob("*") if path.is_file())
        before = _state(service, run.id, attempt_id)
        invalid = '{"summary": "Energy in \\AA units", "findings": []}'

        checked = service.check_submission(attempt_id, review["input_digest"], invalid)

        assert checked["valid"] is False
        assert checked["recorded"] is False
        assert [issue["code"] for issue in checked["validation_report"]["issues"]] == ["json.invalid"]
        assert checked["validation_report"]["output_artifact_digest"] is None
        assert _state(service, run.id, attempt_id) == before
        assert sorted(path.name for path in service.artifacts.root.rglob("*") if path.is_file()) == artifacts_before
        events = [event for event in service.store.list_events(run.id) if event.event_type == "tool.submit_check"]
        assert [event.payload["codes"] for event in events] == [["json.invalid"]]
        assert events[0].payload["valid"] is False

        output = json.dumps(_review_output(review))
        assert service.check_submission(attempt_id, review["input_digest"], output)["valid"] is True
        assert service.store.get_attempt(attempt_id).status == AttemptStatus.RUNNING
        receipt = asyncio.run(service.submit_task(attempt_id, review["input_digest"], output))
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        assert receipt["run_status"] == RunStatus.AWAITING_DECISION


def test_check_matches_submission_validation_issues(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        finding = review_finding(review)
        finding["evidence"][0]["quoted_text"] = "Not in the manuscript."
        output = json.dumps(_review_output(review, findings=[finding]))

        checked = service.check_submission(review["attempt"].id, review["input_digest"], output)
        receipt = asyncio.run(service.submit_task(review["attempt"].id, review["input_digest"], output))

        assert checked["valid"] is False
        assert receipt["attempt"].status == AttemptStatus.FAILED
        assert checked["validation_report"]["issues"] == receipt["validation_report"]["issues"]
        assert checked["output_digest"] == receipt["output_digest"]


def test_check_requires_the_attempt_digest_and_an_active_attempt(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        output = json.dumps(_review_output(review))
        with pytest.raises(StateError, match="input digest"):
            service.check_submission(review["attempt"].id, "0" * 64, output)
        submit(service, review, _review_output(review))
        with pytest.raises(StateError, match="no longer active"):
            service.check_submission(review["attempt"].id, review["input_digest"], output)
        assert all(task.status == TaskStatus.COMPLETED for task in service.store.list_tasks(run.id))


def test_check_reports_a_check_from_a_writer_session(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        prepare_patch(service, run.id)
        # prepare_patch wrote from codex session writing-session.
        lead = claim_check(service, run.id, session="writing-session", client="codex")
        checked = submit_check(service, lead, check_output(service, run.id), check=True)
        assert [issue["code"] for issue in checked["validation_report"]["issues"]] == ["check.session_unconfirmed"]
        assert service.store.get_attempt(lead["attempt"].id).status == AttemptStatus.RUNNING
