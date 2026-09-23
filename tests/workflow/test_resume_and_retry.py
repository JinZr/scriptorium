import asyncio

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus
from scriptorium.errors import InfrastructureError, StateError
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository, review_finding, submit


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
