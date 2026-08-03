import asyncio
import json

import pytest

from scriptorium.domain import AgentRole, PatchStatus, RunStatus, TaskStatus
from scriptorium.errors import InfrastructureError
from scriptorium.service import ScriptoriumService

from ._support import MANUSCRIPT, FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository


class FailingPatchedBuildManager(PdfBuildingManuscriptManager):
    def build(self, workspace, manuscript):
        if "The result is clear." in (workspace / manuscript.main).read_text(encoding="utf-8"):
            raise InfrastructureError("simulated patched LaTeX failure")
        return super().build(workspace, manuscript)


class PdfEvidenceRuntime(FakeAgentRuntime):
    def __init__(
        self,
        quoted_text: str,
        *,
        interrupt_copyedit_once: bool = False,
        verification_quoted_text: str | None = None,
    ) -> None:
        super().__init__(interrupt_copyedit_once=interrupt_copyedit_once)
        self.quoted_text = quoted_text
        self.verification_quoted_text = verification_quoted_text

    def _review_output(self, role, workspace):
        output = super()._review_output(role, workspace)
        if role == AgentRole.SUBSTANTIVE_REVIEW:
            output["findings"][0]["evidence"] = [
                {
                    "source_path": "manuscript.pdf",
                    "page": 1,
                    "quoted_text": self.quoted_text,
                }
            ]
        return output

    def _verification_output(self, task):
        if self.verification_quoted_text is None:
            return super()._verification_output(task)
        return {
            "verdict": "fail",
            "summary": "The patch needs another human-directed revision.",
            "resolved_finding_ids": [],
            "issues": [
                {
                    "title": "Finding remains unresolved",
                    "explanation": "The proposed wording does not fully resolve the finding.",
                    "evidence": [
                        {
                            "source_path": "manuscript.pdf",
                            "page": 1,
                            "quoted_text": self.verification_quoted_text,
                        }
                    ],
                }
            ],
        }


def test_pdf_evidence_quote_matches_text_on_the_cited_page(tmp_path):
    repo = make_repository(tmp_path)
    runtime = PdfEvidenceRuntime("The result\nis clear.")

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        assert started["run"].status == RunStatus.AWAITING_DECISION
        assert service.list_findings(started["run"].id)[0].evidence[0]["quoted_text"] == "The result\nis clear."


def test_fabricated_pdf_evidence_quote_is_rejected(tmp_path):
    repo = make_repository(tmp_path)
    runtime = PdfEvidenceRuntime("This text does not appear on the page.")

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        assert started["run"].status == RunStatus.REVIEWING
        assert service.list_findings(started["run"].id) == []
        task = next(
            task for task in service.database.list_tasks(started["run"].id) if task.role == AgentRole.SUBSTANTIVE_REVIEW
        )
        attempts = service.database.list_attempts(task.id)
        assert len(attempts) == 2
        assert all(
            attempt.error == "invalid structured output: 1 issues; first evidence.pdf_quote_mismatch at "
            "/findings/0/evidence/0/quoted_text"
            for attempt in attempts
        )
        assert all(attempt.validation_report_artifact_digest for attempt in attempts)


def test_legacy_run_preserves_page_only_evidence_through_resume_and_verification(tmp_path, monkeypatch):
    repo = make_repository(tmp_path)
    runtime = PdfEvidenceRuntime(
        "This text does not appear on the page.",
        interrupt_copyedit_once=True,
        verification_quoted_text="This text does not appear on the patched page.",
    )
    manager = PdfBuildingManuscriptManager(repo)

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=manager,
    ) as service:
        freeze_config = service.armarius._freeze_config

        def freeze_legacy_config(project, profile, sources, project_config_text):
            frozen = freeze_config(project, profile, sources, project_config_text)
            frozen["local"]["roles"].pop(AgentRole.VISUAL_TRANSCRIPTION.value)
            frozen["role_routes"].pop(AgentRole.VISUAL_TRANSCRIPTION.value)
            frozen["prompts"].pop(AgentRole.VISUAL_TRANSCRIPTION.value)
            frozen["schemas"].pop("visual_transcription")
            return frozen

        monkeypatch.setattr(service.armarius, "_freeze_config", freeze_legacy_config)
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        run_id = started["run"].id
        tasks = {item["task"].role: item["task"] for item in started["tasks"]}

        assert started["run"].status == RunStatus.REVIEWING
        assert tasks[AgentRole.SUBSTANTIVE_REVIEW].status == TaskStatus.COMPLETED
        assert tasks[AgentRole.COPYEDIT].status == TaskStatus.INTERRUPTED
        assert AgentRole.VISUAL_TRANSCRIPTION not in tasks

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=manager,
    ) as service:
        resumed = asyncio.run(service.resume_run(run_id))

        assert resumed["run"].status == RunStatus.AWAITING_DECISION
        assert runtime.run_calls[AgentRole.SUBSTANTIVE_REVIEW] == 1
        assert runtime.resume_calls == [AgentRole.COPYEDIT]
        evidence = service.list_findings(run_id)[0].evidence[0]
        assert evidence["quoted_text"] == "This text does not appear on the page."

        service.decide_finding(
            service.list_findings(run_id)[0].id,
            "confirm",
            "The typo should be corrected.",
        )
        revised = asyncio.run(service.resume_run(run_id))
        patch_id = revised["patch_ids"][0]
        service.decide_patch(patch_id, "approve", "Verify this exact edit.")

        verified = asyncio.run(service.resume_run(run_id))

        assert verified["run"].status == RunStatus.AWAITING_PATCH_APPROVAL
        verification = service.get_patch(patch_id)["verifications"][0]
        assert verification.result.value == "fail"
        assert runtime.run_calls[AgentRole.VERIFICATION] == 1


def test_full_workflow_preserves_worktree_until_approved_patch_is_applied(tmp_path):
    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime()
    manager = PdfBuildingManuscriptManager(repo)

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=manager,
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        run_id = started["run"].id

        assert started["run"].status == RunStatus.AWAITING_DECISION
        review_prompt = runtime.tasks[AgentRole.SUBSTANTIVE_REVIEW]
        assert "source_path must be the bare relative path from source-map.json" in review_prompt
        assert "start_line, end_line, source_digest, and quoted_text must be supplied" in review_prompt
        assert 'use source_path "manuscript.pdf", set page to a valid 1-based PDF page number' in review_prompt
        assert "copy quoted_text verbatim from that page's native text layer" in review_prompt
        assert "never line-anchor .pdf files or other graphics/binary assets" in review_prompt
        assert "ReviewOutput JSON object with no prose before or after it" in review_prompt
        frozen_route = started["run"].frozen_config["local"]["routes"]["primary"]
        assert frozen_route["runtime"] == "codex"
        assert frozen_route["runtime_version"] == "0.144.4"
        assert "runtime" not in started["run"].frozen_config
        assert not (repo / ".scriptorium" / "runs" / run_id / "snapshot" / "main.pdf").exists()
        assert repo.joinpath("main.tex").read_text(encoding="utf-8") == MANUSCRIPT
        source_digest = started["run"].frozen_config["sources"][0]["digest"]
        assert service.database.get_artifact(source_digest).digest == source_digest
        bundle_manifest = json.loads(
            (repo / ".scriptorium" / "runs" / run_id / "bundle" / "manifest.json").read_text(encoding="utf-8")
        )
        page_digest = bundle_manifest["pages"][0]["digest"]
        assert service.database.get_artifact(page_digest).digest == page_digest

        findings = service.list_findings(run_id)
        assert len(findings) == 1
        service.decide_finding(findings[0].id, "confirm", "The typo should be corrected.")

        revised = asyncio.run(service.resume_run(run_id))
        assert revised["run"].status == RunStatus.AWAITING_PATCH_APPROVAL
        revision_prompt = runtime.tasks[AgentRole.REVISION]
        assert "path must be the bare relative path from source-map.json" in revision_prompt
        assert "source_digest must match source-map.json" in revision_prompt
        assert "before must reproduce the exact current text of the cited lines" in revision_prompt
        assert repo.joinpath("main.tex").read_text(encoding="utf-8") == MANUSCRIPT

        patch_id = revised["patch_ids"][0]
        patch_view = service.get_patch(patch_id)
        assert "-The result is teh clear." in patch_view["diff"]
        assert "+The result is clear." in patch_view["diff"]
        service.decide_patch(patch_id, "approve", "The exact replacement is correct.")

        verified = asyncio.run(service.resume_run(run_id))
        assert verified["run"].status == RunStatus.READY_TO_APPLY
        verification_prompt = runtime.tasks[AgentRole.VERIFICATION]
        assert "source_path must be the bare relative path from source-map.json" in verification_prompt
        assert "start_line, end_line, source_digest, and quoted_text must be supplied" in verification_prompt
        assert 'use source_path "manuscript.pdf", set page to a valid 1-based PDF page number' in verification_prompt
        assert "copy quoted_text verbatim from the cited page" in verification_prompt
        assert "never line-anchor .pdf files or other graphics/binary assets" in verification_prompt
        assert "VerificationOutput JSON object with no prose before or after it" in verification_prompt
        assert repo.joinpath("main.tex").read_text(encoding="utf-8") == MANUSCRIPT
        assert service.evaluate_gate(run_id)["passed"] is False

        applied = service.apply_patch(patch_id)
        assert applied.status == PatchStatus.APPLIED
        assert "The result is clear." in repo.joinpath("main.tex").read_text(encoding="utf-8")

        gate = service.evaluate_gate(run_id)
        report = service.render_report(run_id, "markdown")
        assert gate["passed"] is True
        assert "Gate: `pass`" in report
        assert service.get_run(run_id)["run"].status == RunStatus.COMPLETED


def test_failed_patched_build_does_not_modify_author_worktree(tmp_path):
    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=FailingPatchedBuildManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        run_id = started["run"].id
        finding = service.list_findings(run_id)[0]
        service.decide_finding(finding.id, "confirm", "The typo should be corrected.")

        with pytest.raises(InfrastructureError, match="simulated patched LaTeX failure"):
            asyncio.run(service.resume_run(run_id))

        assert service.get_run(run_id)["run"].status == RunStatus.FAILED
        assert repo.joinpath("main.tex").read_text(encoding="utf-8") == MANUSCRIPT
