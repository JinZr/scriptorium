import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus, TaskStatus
from scriptorium.errors import InfrastructureError, StateError
from scriptorium.service import ScriptoriumService

from ._support import (
    PdfBuildingManuscriptManager,
    approve_plan,
    check_output,
    claim,
    claim_check,
    complete_reviews,
    export_writing,
    make_repository,
    prepare_patch,
    review_finding,
    submit,
    submit_check,
    submit_writing,
    writing_task,
)


def test_running_attempt_is_durable_across_service_restarts(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="same-session")
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        repeated = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="same-session")
        assert repeated["attempt"].id == first["attempt"].id
        with pytest.raises(StateError, match="different session"):
            claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="other-session")
        receipt = submit(service, repeated, {"summary": "Reviewed the frozen text.", "findings": []})
        assert receipt["run_status"] == RunStatus.AWAITING_DECISION


def test_first_accepted_review_after_retry_keeps_distinct_findings(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review", "copyedit"))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        failed = submit(service, first, {"summary": "Invalid output.", "findings": [{"wrong": "shape"}]})
        assert failed["attempt"].status == AttemptStatus.FAILED
        asyncio.run(service.retry_task(run.id, first["task"].id))
        second = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="corrected-session")
        finding = review_finding(second)
        submit(
            service,
            second,
            {
                "summary": "Two separate concerns on the same evidence.",
                "findings": [finding, {**finding, "explanation": "A different concern about that sentence."}],
            },
        )
        findings = service.list_findings(run.id)
        assert len(findings) == 2
        asyncio.run(service.resume_run(run.id))
        assert service.list_findings(run.id) == findings


def test_resume_replays_accepted_findings_after_crash(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        service.armarius.submit_task(
            first["attempt"].id,
            first["input_digest"],
            json.dumps(
                {
                    "summary": "Found a typo.",
                    "findings": [review_finding(first)],
                    "claim_checks": [
                        {
                            "claim_index": 0,
                            "evidence": [{"source_path": "manuscript.pdf", "page": 1}],
                            "critical_question": "Does the wording support the claim?",
                            "countercheck": "Checked the reported result.",
                            "stated_scope": "As stated in the manuscript.",
                            "check_type": "design_and_analysis",
                            "question_answer": "no",
                            "exceptions": [],
                            "assessment": "finding",
                            "finding_indices": [0],
                        }
                    ],
                    "claim_inventory": [
                        {
                            "claim": "The reported result is clear.",
                            "claim_anchor": {"source_path": "manuscript.pdf", "page": 1},
                            "prominence": "headline",
                        }
                    ],
                    "verdict": {
                        "recommendation": "major_revision",
                        "decisive_questions": ["Does the wording support the claim?"],
                    },
                    "limitations": [],
                }
            ),
        )
        assert service.list_findings(run.id) == []

    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        resumed = asyncio.run(service.resume_run(run.id))
        assert resumed["run"].status == RunStatus.AWAITING_DECISION
        assert service.store.get_task(first["task"].id).status == TaskStatus.COMPLETED
        assert len(service.list_findings(run.id)) == 1
        assert service.list_findings(run.id)[0].attempt_id == first["attempt"].id


@pytest.mark.parametrize("with_finding", [False, True])
def test_duplicate_submission_uses_attempt_state_after_run_lock(tmp_path, monkeypatch, with_finding):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with (
        ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as first,
        ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as second,
    ):
        run = asyncio.run(first.start_run("HEAD", "quick"))["run"]
        review = claim(first, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        output = {
            "summary": "Reviewed the frozen text.",
            "findings": [review_finding(review)] if with_finding else [],
        }
        waiting = threading.Event()
        release = threading.Event()
        original = second._run_operation

        @contextmanager
        def delayed_lock(*args, **kwargs):
            waiting.set()
            assert release.wait(5)
            with original(*args, **kwargs):
                yield

        monkeypatch.setattr(second, "_run_operation", delayed_lock)
        with ThreadPoolExecutor(max_workers=1) as pool:
            duplicate = pool.submit(submit, second, review, output)
            assert waiting.wait(5)
            try:
                accepted = submit(first, review, output)
            finally:
                release.set()
            repeated = duplicate.result(timeout=5)

        assert accepted["run_status"] == RunStatus.AWAITING_DECISION
        assert repeated["run_status"] == RunStatus.AWAITING_DECISION
        assert repeated["output_digest"] == accepted["output_digest"]
        assert first.store.get_run(run.id).status == RunStatus.AWAITING_DECISION
        assert len(first.list_findings(run.id)) == int(with_finding)
        assert [event.event_type for event in first.store.list_events(run.id)].count("attempt.finished") == 1


def test_invalid_anchor_requires_explicit_retry_with_saved_diagnostics(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        finding = review_finding(first)
        finding["evidence"][0]["source_digest"] = "0" * 64
        failed = submit(service, first, {"summary": "Found a typo.", "findings": [finding]})
        assert failed["attempt"].status == AttemptStatus.FAILED
        assert failed["validation_report"]
        assert service.list_findings(run.id) == []
        task = service.store.list_tasks(run.id)[0]
        asyncio.run(service.retry_task(run.id, task.id))
        second = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="correction-session")
        assert "source_digest" in second["prompt"]
        assert second["attempt"].ordinal == 2
        assert second["input_digest"] != first["input_digest"]
        with pytest.raises(StateError, match="input digest"):
            asyncio.run(service.submit_task(second["attempt"].id, first["input_digest"], "{}"))
        receipt = submit(service, second, {"summary": "Corrected the anchor.", "findings": [review_finding(second)]})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        assert len(service.list_findings(run.id)) == 1


def test_abandoned_correction_claim_preserves_rejection_diagnostics(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        failed = submit(service, first, {"summary": "Invalid output", "findings": [{"wrong": "shape"}]})
        task = service.store.list_tasks(run.id)[0]
        asyncio.run(service.retry_task(run.id, task.id))
        correction = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="correction-session")
        assert failed["attempt"].id in correction["prompt"]
        assert correction["input_digest"] != first["input_digest"]

        asyncio.run(service.retry_task(run.id, task.id, correction["attempt"].id, "session lost"))
        resumed = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="new-session")
        assert resumed["prompt"] == correction["prompt"]
        assert resumed["input_digest"] == correction["input_digest"]
        assert resumed["attempt"].ordinal == 3
        with pytest.raises(StateError, match="superseded"):
            submit(service, correction, {"summary": "Late output", "findings": []})


def test_corrupt_validation_report_blocks_retry(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        failed = submit(service, first, {"summary": "Invalid output", "findings": [{"wrong": "shape"}]})
        digest = failed["attempt"].validation_report_artifact_digest
        assert digest
        service.artifacts.path_for(digest).write_bytes(b"corrupt")
        task = service.store.list_tasks(run.id)[0]
        asyncio.run(service.retry_task(run.id, task.id))
        with pytest.raises(InfrastructureError, match="report|artifact"):
            claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="correction-session")
        assert len(service.store.list_attempts(task.id)) == 1


def test_corrupt_attempt_prompt_blocks_submission(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        service.artifacts.path_for(review["attempt"].prompt_digest).write_bytes(b"corrupt")
        with pytest.raises(InfrastructureError, match="artifact|prompt"):
            submit(service, review, {"summary": "Reviewed the text.", "findings": []})
        assert service.store.get_attempt(review["attempt"].id).status == AttemptStatus.RUNNING
        assert service.list_findings(run.id) == []


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_attempt_schema_blocks_submission(tmp_path, damage):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        path = service.artifacts.path_for(review["attempt"].schema_digest)
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"corrupt")
        with pytest.raises(InfrastructureError, match="schema artifact"):
            service.show_task(review["attempt"].id)
        with pytest.raises(InfrastructureError, match="schema artifact"):
            submit(service, review, {"summary": "Reviewed the text.", "findings": []})
        assert service.store.get_attempt(review["attempt"].id).status == AttemptStatus.RUNNING
        assert service.list_findings(run.id) == []


def test_explicit_abandonment_retries_only_the_named_running_attempt(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="lost-session")
        task = first["task"]
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        with pytest.raises(StateError, match="current running attempt"):
            asyncio.run(service.retry_task(run.id, task.id, "wrong-attempt", "conversation lost"))
        assert service.store.get_attempt(first["attempt"].id).status == AttemptStatus.RUNNING
        asyncio.run(service.retry_task(run.id, task.id, first["attempt"].id, "conversation lost"))
        assert service.store.get_attempt(first["attempt"].id).status == AttemptStatus.INTERRUPTED
        with pytest.raises(StateError, match="superseded"):
            submit(service, first, {"summary": "Late output.", "findings": []})
        second = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="new-session")
        assert second["attempt"].ordinal == 2
        assert (
            submit(service, second, {"summary": "Reviewed the manuscript.", "findings": []})["attempt"].status
            == AttemptStatus.COMPLETED
        )


def test_a_waiver_during_writing_returns_the_run_to_planning(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        first = review_finding(review)
        second = {**first, "title": "Second issue", "claim": "The same sentence needs another check."}
        submit(service, review, {"summary": "Two issues.", "findings": [first, second]})
        findings = service.list_findings(run.id)
        assert len(findings) == 2
        service.decide_findings([finding.id for finding in findings], "confirm", "Both need writing.")
        asyncio.run(service.resume_run(run.id))
        approve_plan(
            service,
            run.id,
            {
                "result": (["main.tex:3-3"], [findings[0].id], "Correct the typo."),
                "setup": (["main.tex:1-1"], [findings[1].id], "Keep the document class."),
            },
        )
        original, export = export_writing(service, run.id, "result")
        pending = writing_task(service, run.id, "setup")
        service.decide_finding(findings[0].id, "waive", "No longer required")
        assert service.store.get_attempt(original["attempt"].id).status == AttemptStatus.INTERRUPTED
        assert service.store.get_task(pending.id).status == TaskStatus.CANCELLED
        assert service.store.get_run(run.id).status == RunStatus.PLANNING
        with pytest.raises(StateError, match="superseded"):
            submit_writing(service, original, export, {1: "The result is clear."})
        with pytest.raises(StateError, match="cannot be retried while run is planning"):
            asyncio.run(service.retry_task(run.id, original["task"].id))
        assert asyncio.run(service.resume_run(run.id))["run"].status == RunStatus.PLANNING
        assert service.list_tasks(run.id)["next_actions"] == [
            {"command": "plan approve", "run_id": run.id, "requires_human_decision": True}
        ]
        approve_plan(service, run.id, {"setup": (["main.tex:1-1"], [findings[1].id], "Keep the document class.")})
        fresh, _ = export_writing(service, run.id, "setup", session="new-session")
        assert findings[0].id not in fresh["prompt"]
        assert findings[1].id in fresh["prompt"]


def test_waiving_the_last_confirmed_finding_during_writing_completes_the_run(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        complete_reviews(service, run.id)
        finding_id = service.list_findings(run.id)[0].id
        service.decide_finding(finding_id, "confirm", "Correct the typo.")
        asyncio.run(service.resume_run(run.id))
        approve_plan(service, run.id)
        service.decide_finding(finding_id, "waive", "The author keeps the sentence.")
        assert service.store.get_run(run.id).status == RunStatus.PLANNING
        assert service.list_tasks(run.id)["next_actions"] == [{"command": "run resume", "run_id": run.id}]
        assert asyncio.run(service.resume_run(run.id))["run"].status == RunStatus.COMPLETED


def test_waiving_the_last_confirmed_finding_during_the_check_interrupts_it_and_completes_the_run(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        _, finding_id = prepare_patch(service, run.id)
        lead = claim_check(service, run.id)
        service.decide_finding(finding_id, "waive", "The patch is no longer required")
        assert service.store.get_attempt(lead["attempt"].id).status == AttemptStatus.INTERRUPTED
        assert service.store.get_run(run.id).status == RunStatus.PLANNING
        with pytest.raises(StateError, match="superseded"):
            submit_check(service, lead, check_output(service, run.id))
        asyncio.run(service.resume_run(run.id))
        assert service.store.get_run(run.id).status == RunStatus.COMPLETED
