import asyncio

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
