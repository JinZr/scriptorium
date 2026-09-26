import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus, TaskStatus
from scriptorium.errors import InfrastructureError, StateError
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository, prepare_verification, review_finding, submit


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


@pytest.mark.parametrize("artifact_damage", ["missing", "corrupt"])
def test_partial_review_continues_across_processes_without_replacing_findings(tmp_path, artifact_damage):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="first-session")
        task_id = first["task"].id
        finding = review_finding(first)
        partial = {
            "summary": "The text has a typo; the page still needs inspection.",
            "findings": [finding],
            "scope": {
                "completion": "partial",
                "checked": [{"source_path": "main.tex", "start_line": 1, "end_line": 4}],
                "outstanding": [{"source_path": "manuscript.pdf", "page": 1}],
                "limitations": ["Rendered page not inspected."],
            },
        }
        first_receipt = submit(service, first, partial)
        assert first_receipt["run_status"] == RunStatus.REVIEWING
        assert first_receipt["next_actions"] == [{"command": "run continue", "run_id": run.id, "task_id": task_id}]
        assert submit(service, first, partial)["output_digest"] == first_receipt["output_digest"]
        assert not service.evaluate_gate(run.id)["conditions"]["required_reviews_completed"]
        original_finding = service.list_findings(run.id)[0]
        asyncio.run(service.continue_review(run.id, task_id))
        assert service.database.get_task(task_id).status == TaskStatus.PENDING
        with pytest.raises(StateError, match="superseded"):
            submit(service, first, partial)

    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        second = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="second-session")
        assert second["attempt"].ordinal == 2
        assert second["input_digest"] != first["input_digest"]
        assert "manuscript.pdf" in second["prompt"]
        assert original_finding.id in second["prompt"]
        assert service.page_task(second["attempt"].id, 1)["page"] == 1
        finished = submit(
            service,
            second,
            {
                "summary": "Reviewed the remaining page; the typo remains.",
                "findings": [finding],
                "scope": {
                    "completion": "complete",
                    "checked": [
                        {"source_path": "main.tex", "start_line": 1, "end_line": 4},
                        {"source_path": "manuscript.pdf", "page": 1},
                    ],
                    "outstanding": [],
                    "limitations": [],
                },
            },
        )
        assert finished["run_status"] == RunStatus.AWAITING_DECISION
        assert service.evaluate_gate(run.id)["conditions"]["required_reviews_completed"]
        assert finished["next_actions"] == [
            {"command": "finding list", "run_id": run.id, "requires_human_decision": True}
        ]
        assert service.list_findings(run.id) == [original_finding]
        attempts = service.database.list_attempts(task_id)
        assert [attempt.status for attempt in attempts] == [AttemptStatus.COMPLETED, AttemptStatus.COMPLETED]
        assert attempts[0].output_artifact_digest == first_receipt["output_digest"]
        service.decide_finding(original_finding.id, "reject", "No revision needed after review")
        assert service.database.get_finding(original_finding.id).status.value == "rejected"
        asyncio.run(service.resume_run(run.id))
        assert service.evaluate_gate(run.id)["passed"]
        earlier_output = service.artifacts.path_for(first_receipt["output_digest"])
        if artifact_damage == "missing":
            earlier_output.unlink()
        else:
            earlier_output.write_text("corrupt", encoding="utf-8")
        gate = service.evaluate_gate(run.id)
        assert not gate["conditions"]["review_artifacts_valid"]
        assert first["attempt"].id in gate["errors"][0]


def test_partial_review_invalid_continuation_preserves_prior_output_and_diagnostics(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        task_id = first["task"].id
        submit(
            service,
            first,
            {
                "summary": "Review incomplete.",
                "findings": [review_finding(first)],
                "scope": {"completion": "unknown", "checked": [], "outstanding": [], "limitations": ["Page unread."]},
            },
        )
        asyncio.run(service.continue_review(run.id, task_id))
        second = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="second-session")
        failed = submit(
            service,
            second,
            {
                "summary": "Invalid continuation.",
                "findings": [],
                "scope": {
                    "completion": "complete",
                    "checked": [{"source_path": "missing.tex"}],
                    "outstanding": [],
                    "limitations": [],
                },
            },
        )
        assert failed["attempt"].status == AttemptStatus.FAILED
        assert len(service.list_findings(run.id)) == 1
        asyncio.run(service.retry_task(run.id, task_id))
        third = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="third-session")
        assert third["attempt"].ordinal == 3
        assert "Page unread" in third["prompt"]
        assert "scope.path_unknown" in third["prompt"]
        submit(
            service,
            third,
            {
                "summary": "Review complete.",
                "findings": [],
                "scope": {"completion": "complete", "checked": [], "outstanding": [], "limitations": []},
            },
        )
        assert service.database.get_run(run.id).status == RunStatus.AWAITING_DECISION
        assert len(service.list_findings(run.id)) == 1


def test_prior_decision_survives_continuing_a_review_from_decision_stage(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        task_id = first["task"].id
        submit(
            service,
            first,
            {
                "summary": "Text checked, page outstanding.",
                "findings": [review_finding(first)],
                "scope": {
                    "completion": "partial",
                    "checked": [{"source_path": "main.tex"}],
                    "outstanding": [{"source_path": "manuscript.pdf", "page": 1}],
                    "limitations": [],
                },
            },
        )
        original = service.list_findings(run.id)[0]
        service.database.update_run(run.id, RunStatus.AWAITING_DECISION)
        service.decide_finding(original.id, "confirm", "The text needs correction")
        reopened = asyncio.run(service.continue_review(run.id, task_id))
        assert reopened["run_status"] == RunStatus.REVIEWING
        assert service.database.get_finding(original.id).status.value == "confirmed"
        second = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="continued-session")
        submit(
            service,
            second,
            {
                "summary": "Page checked; issue still stands.",
                "findings": [],
                "scope": {"completion": "complete", "checked": [], "outstanding": [], "limitations": []},
            },
        )
        assert service.database.get_run(run.id).status == RunStatus.AWAITING_DECISION
        assert service.database.get_finding(original.id).status.value == "confirmed"
        with pytest.raises(StateError, match="no incomplete accepted review"):
            asyncio.run(service.continue_review(run.id, task_id))


def test_resume_does_not_advance_older_incomplete_decision_stage(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        submit(
            service,
            first,
            {
                "summary": "Review scope uncertain.",
                "findings": [],
                "scope": {"completion": "unknown", "checked": [], "outstanding": [], "limitations": []},
            },
        )
        service.database.update_run(run.id, RunStatus.AWAITING_DECISION)
        resumed = asyncio.run(service.resume_run(run.id))
        assert resumed["run"].status == RunStatus.REVIEWING
        assert resumed["tasks"][0]["task"].status == TaskStatus.COMPLETED
        assert service.list_tasks(run.id)["next_actions"] == [
            {"command": "run continue", "run_id": run.id, "task_id": first["task"].id}
        ]


def test_continue_replays_accepted_findings_before_reopening_after_crash(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        service.armarius.submit_task(
            first["attempt"].id,
            first["input_digest"],
            json.dumps(
                {
                    "summary": "Found a typo; page outstanding.",
                    "findings": [review_finding(first)],
                    "claim_checks": [
                        {
                            "claim": "The reported result is clear.",
                            "evidence": [{"source_path": "manuscript.pdf", "page": 1}],
                            "critical_question": "Does the wording support the claim?",
                            "countercheck": "Checked the reported result.",
                            "assessment": "finding",
                            "finding_indices": [0],
                        }
                    ],
                    "scope": {
                        "completion": "partial",
                        "checked": [{"source_path": "main.tex"}],
                        "outstanding": [{"source_path": "manuscript.pdf", "page": 1}],
                        "limitations": [],
                    },
                }
            ),
        )
        assert service.list_findings(run.id) == []

    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        asyncio.run(service.continue_review(run.id, first["task"].id))
        assert service.database.get_task(first["task"].id).status == TaskStatus.PENDING
        assert len(service.list_findings(run.id)) == 1
        assert service.list_findings(run.id)[0].attempt_id == first["attempt"].id


def test_cancelled_continuation_rejects_late_submission_and_keeps_accepted_result(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        accepted = submit(
            service,
            first,
            {
                "summary": "Initial review incomplete.",
                "findings": [review_finding(first)],
                "scope": {"completion": "partial", "checked": [], "outstanding": [], "limitations": []},
            },
        )
        asyncio.run(service.continue_review(run.id, first["task"].id))
        second = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="later-session")
        service.cancel_run(run.id, "Stop this run")
        with pytest.raises(StateError, match="no longer active"):
            submit(service, second, {"summary": "Too late.", "findings": []})
        assert service.database.list_attempts(first["task"].id)[0].output_artifact_digest == accepted["output_digest"]
        assert len(service.list_findings(run.id)) == 1
        assert not service.evaluate_gate(run.id)["passed"]


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
        assert first.database.get_run(run.id).status == RunStatus.AWAITING_DECISION
        assert len(first.list_findings(run.id)) == int(with_finding)
        assert [event.event_type for event in first.database.list_events(run.id)].count("attempt.finished") == 1


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
        task = service.database.list_tasks(run.id)[0]
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
        task = service.database.list_tasks(run.id)[0]
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
        task = service.database.list_tasks(run.id)[0]
        asyncio.run(service.retry_task(run.id, task.id))
        with pytest.raises(InfrastructureError, match="report|artifact"):
            claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="correction-session")
        assert len(service.database.list_attempts(task.id)) == 1


def test_corrupt_attempt_prompt_blocks_submission(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        service.artifacts.path_for(review["attempt"].prompt_digest).write_bytes(b"corrupt")
        with pytest.raises(InfrastructureError, match="artifact|prompt"):
            submit(service, review, {"summary": "Reviewed the text.", "findings": []})
        assert service.database.get_attempt(review["attempt"].id).status == AttemptStatus.RUNNING
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
        assert service.database.get_attempt(review["attempt"].id).status == AttemptStatus.RUNNING
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
        assert service.database.get_attempt(first["attempt"].id).status == AttemptStatus.RUNNING
        asyncio.run(service.retry_task(run.id, task.id, first["attempt"].id, "conversation lost"))
        assert service.database.get_attempt(first["attempt"].id).status == AttemptStatus.INTERRUPTED
        with pytest.raises(StateError, match="superseded"):
            submit(service, first, {"summary": "Late output.", "findings": []})
        second = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="new-session")
        assert second["attempt"].ordinal == 2
        assert (
            submit(service, second, {"summary": "Reviewed the manuscript.", "findings": []})["attempt"].status
            == AttemptStatus.COMPLETED
        )


def test_later_waiver_invalidates_active_revision_and_prepares_new_context(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        first = review_finding(review)
        second = {**first, "title": "Second issue", "claim": "The same sentence needs another check."}
        submit(service, review, {"summary": "Two issues.", "findings": [first, second]})
        findings = service.list_findings(run.id)
        assert len(findings) == 2
        for finding in findings:
            service.decide_finding(finding.id, "confirm", "Requires a revision")
        asyncio.run(service.resume_run(run.id))
        original = claim(service, run.id, AgentRole.REVISION)
        service.decide_finding(findings[0].id, "waive", "No longer required")
        assert service.database.get_attempt(original["attempt"].id).status == AttemptStatus.INTERRUPTED
        with pytest.raises(StateError, match="superseded"):
            submit(service, original, {"summary": "Old scope.", "edits": []})
        with pytest.raises(StateError, match="context changed"):
            asyncio.run(service.retry_task(run.id, original["task"].id))
        asyncio.run(service.resume_run(run.id))
        tasks = [task for task in service.database.list_tasks(run.id) if task.stage == "revision"]
        assert len(tasks) == 2
        replacement = next(task for task in tasks if task.id != original["task"].id)
        assert replacement.status == TaskStatus.PENDING
        fresh = service.claim_task(replacement.id, "codex", "test-model", "max", "new-session", "host")
        assert findings[0].id not in fresh["prompt"]
        assert findings[1].id in fresh["prompt"]


def test_later_waiver_invalidates_active_verification(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        _, finding_id = prepare_verification(service, run.id)
        verification = claim(service, run.id, AgentRole.VERIFICATION, session="fresh-verifier")
        service.decide_finding(finding_id, "waive", "The patch is no longer required")
        assert service.database.get_attempt(verification["attempt"].id).status == AttemptStatus.INTERRUPTED
        with pytest.raises(StateError, match="superseded"):
            submit(
                service,
                verification,
                {"verdict": "pass", "summary": "Old scope", "resolved_finding_ids": [], "issues": []},
            )
        asyncio.run(service.resume_run(run.id))
        assert service.database.get_run(run.id).status == RunStatus.COMPLETED
