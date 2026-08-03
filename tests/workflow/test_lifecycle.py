import asyncio
from hashlib import sha256
import json

import pytest

from scriptorium.domain import AgentRole, PatchStatus, RunStatus, TaskStatus
from scriptorium.errors import InfrastructureError
from scriptorium.service import ScriptoriumService
from scriptorium.workflow import Armarius

from ._support import MANUSCRIPT, FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository


class FailingPatchedBuildManager(PdfBuildingManuscriptManager):
    def build(self, workspace, manuscript):
        if "The result is clear." in (workspace / manuscript.main).read_text(encoding="utf-8"):
            raise InfrastructureError("simulated patched LaTeX failure")
        return super().build(workspace, manuscript)


class PdfEvidenceRuntime(FakeAgentRuntime):
    def __init__(
        self,
        *,
        include_quote: bool = False,
        interrupt_copyedit_once: bool = False,
        verification_page_issue: bool = False,
    ) -> None:
        super().__init__(interrupt_copyedit_once=interrupt_copyedit_once)
        self.include_quote = include_quote
        self.verification_page_issue = verification_page_issue

    def _review_output(self, role, workspace):
        output = super()._review_output(role, workspace)
        if role == AgentRole.SUBSTANTIVE_REVIEW:
            evidence = {"source_path": "manuscript.pdf", "page": 1}
            if self.include_quote:
                evidence["quoted_text"] = "This text must not be accepted as a page quote."
            output["findings"][0]["evidence"] = [evidence]
        return output

    def _verification_output(self, task):
        if not self.verification_page_issue:
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
                        }
                    ],
                }
            ],
        }


def test_pdf_page_evidence_anchors_the_cited_rendered_page(tmp_path):
    repo = make_repository(tmp_path)
    runtime = PdfEvidenceRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        assert started["run"].status == RunStatus.AWAITING_DECISION
        assert service.list_findings(started["run"].id)[0].evidence[0] == {
            "source_path": "manuscript.pdf",
            "page": 1,
        }


def test_new_run_does_not_freeze_an_unused_visual_transcription_route(tmp_path):
    repo = make_repository(tmp_path)
    local_path = repo / ".scriptorium" / "config.toml"
    local_path.write_text(
        local_path.read_text(encoding="utf-8").replace(
            'visual_transcription = "primary"',
            'visual_transcription = "visual"',
        )
        + (
            "\n[routes.visual]\n"
            'model_provider = "ollama"\n'
            'model = "unused-visual-model"\n'
            "input_usd_per_million = 0\n"
            "output_usd_per_million = 0\n"
        ),
        encoding="utf-8",
    )

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: FakeAgentRuntime(),
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        run = asyncio.run(service.start_run("HEAD", "quick", None))["run"]

    assert "visual_transcription" not in run.frozen_config["local"]["roles"]
    assert "visual" not in run.frozen_config["local"]["routes"]
    assert "visual_transcription" not in run.frozen_config["schemas"]


def test_pdf_page_evidence_with_quoted_text_is_rejected(tmp_path):
    repo = make_repository(tmp_path)
    runtime = PdfEvidenceRuntime(include_quote=True)

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
            "first schema.cross_field at /findings/0/evidence/0" in (attempt.error or "") for attempt in attempts
        )
        assert all(attempt.validation_report_artifact_digest for attempt in attempts)


def test_page_only_evidence_survives_resume_and_patched_verification(tmp_path):
    repo = make_repository(tmp_path)
    runtime = PdfEvidenceRuntime(
        interrupt_copyedit_once=True,
        verification_page_issue=True,
    )
    manager = PdfBuildingManuscriptManager(repo)

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=manager,
    ) as service:
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
        assert evidence == {"source_path": "manuscript.pdf", "page": 1}

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


def test_full_workflow_preserves_worktree_until_approved_patch_is_applied(tmp_path, monkeypatch):
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
        assert "Frozen evidence anchor contract digest:" in review_prompt
        assert "bare source_path from source-map.json" in review_prompt
        assert 'output only source_path "manuscript.pdf" and a 1-based page' in review_prompt
        assert "Only sources marked text_anchorable may be line-anchored" in review_prompt
        assert "exact page-image read_path" in review_prompt
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
        source_map = json.loads(
            (repo / ".scriptorium" / "runs" / run_id / "bundle" / "source-map.json").read_text(encoding="utf-8")
        )
        anchor_record = started["run"].frozen_config["evidence_anchor_contract"]
        assert source_map["contract_digest"] == anchor_record["digest"]
        assert all(
            f"Frozen evidence anchor contract digest: {anchor_record['digest']}" in template["content"]
            for template in anchor_record["prompt_templates"].values()
        )
        monkeypatch.setattr(
            Armarius,
            "_revision_prompt_template",
            staticmethod(lambda role_prompt, contract: "changed current revision renderer"),
        )
        monkeypatch.setattr(
            Armarius,
            "_verification_prompt_template",
            staticmethod(lambda role_prompt, contract: "changed current verification renderer"),
        )
        page_digest = bundle_manifest["pages"][0]["digest"]
        assert service.database.get_artifact(page_digest).digest == page_digest

        findings = service.list_findings(run_id)
        assert len(findings) == 1
        service.decide_finding(findings[0].id, "confirm", "The typo should be corrected.")

        revised = asyncio.run(service.resume_run(run_id))
        assert revised["run"].status == RunStatus.AWAITING_PATCH_APPROVAL
        revision_prompt = runtime.tasks[AgentRole.REVISION]
        assert "changed current revision renderer" not in revision_prompt
        assert "Each edit path must be a bare source_path marked text_anchorable" in revision_prompt
        assert "two allowed final-line-terminator forms" in revision_prompt
        assert repo.joinpath("main.tex").read_text(encoding="utf-8") == MANUSCRIPT

        patch_id = revised["patch_ids"][0]
        patch_view = service.get_patch(patch_id)
        assert "-The result is teh clear." in patch_view["diff"]
        assert "+The result is clear." in patch_view["diff"]
        service.decide_patch(patch_id, "approve", "The exact replacement is correct.")

        verified = asyncio.run(service.resume_run(run_id))
        assert verified["run"].status == RunStatus.READY_TO_APPLY
        verification_prompt = runtime.tasks[AgentRole.VERIFICATION]
        assert "changed current verification renderer" not in verification_prompt
        assert "their evidence are historical context" in verification_prompt
        assert "current patched workspace source-map.json" in verification_prompt
        assert "visual-only issues" in verification_prompt
        assert "VerificationOutput JSON object with no prose before or after it" in verification_prompt
        verification_map = json.loads(
            (
                repo / ".scriptorium" / "runs" / run_id / "verifications" / patch_id / "bundle" / "source-map.json"
            ).read_text(encoding="utf-8")
        )
        patched_source = next(item for item in verification_map["sources"] if item["source_path"] == "main.tex")
        assert (
            patched_source["source_digest"]
            == sha256(
                (
                    repo
                    / ".scriptorium"
                    / "runs"
                    / run_id
                    / "verifications"
                    / patch_id
                    / "bundle"
                    / "sources"
                    / "main.tex"
                ).read_bytes()
            ).hexdigest()
        )
        assert patched_source["source_digest"] != findings[0].evidence[0]["source_digest"]
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
