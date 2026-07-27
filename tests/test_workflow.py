import asyncio
from collections import Counter
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess

import fitz
import pytest

from scriptorium.domain import AgentRole, AttemptStatus, PatchStatus, RunStatus, TaskStatus
from scriptorium.errors import InfrastructureError
from scriptorium.manuscript import BuildResult, ManuscriptManager
from scriptorium.runtime import AgentResult, AgentUsage
from scriptorium.service import ScriptoriumService

MANUSCRIPT = "\\documentclass{article}\n" "\\begin{document}\n" "The result is teh clear.\n" "\\end{document}\n"


class PdfBuildingManuscriptManager(ManuscriptManager):
    def build(self, workspace, manuscript):
        pdf_path = workspace / Path(manuscript.main).with_suffix(".pdf")
        pdf_path.unlink(missing_ok=True)
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 72), "The result is clear.")
            document.save(pdf_path)
        finally:
            document.close()
        return BuildResult(pdf_path=pdf_path, log="fake LaTeX build succeeded")


class FailingPatchedBuildManager(PdfBuildingManuscriptManager):
    def build(self, workspace, manuscript):
        if "The result is clear." in (workspace / manuscript.main).read_text(encoding="utf-8"):
            raise InfrastructureError("simulated patched LaTeX failure")
        return super().build(workspace, manuscript)


class FakeAgentRuntime:
    def __init__(
        self,
        *,
        interrupt_copyedit_once=False,
        invalid_substantive_once=False,
        runtime_name="codex",
        runtime_version="0.144.4",
        model="fake-model",
        provider="ollama",
    ):
        self.interrupt_copyedit_once = interrupt_copyedit_once
        self.invalid_substantive_once = invalid_substantive_once
        self.runtime_name = runtime_name
        self.runtime_version = runtime_version
        self.model = model
        self.provider = provider
        self.run_calls = Counter()
        self.resume_calls = []
        self.workspaces = {}
        self.session_dirs = {}
        self.session_dir_calls = []

    async def run_agent(self, task, role, workspace, schema, session_dir):
        self.run_calls[role] += 1
        self.workspaces[role] = workspace
        self.session_dirs[role] = session_dir
        self.session_dir_calls.append((role, session_dir))
        ordinal = self.run_calls[role]
        if role == AgentRole.COPYEDIT and self.interrupt_copyedit_once and ordinal == 1:
            return self._result(role, ordinal, "interrupted", None)
        if role in {AgentRole.SUBSTANTIVE_REVIEW, AgentRole.COPYEDIT}:
            output = self._review_output(role, workspace)
            if role == AgentRole.SUBSTANTIVE_REVIEW and self.invalid_substantive_once and ordinal == 1:
                output["findings"][0]["evidence"][0]["source_digest"] = "0" * 64
        elif role == AgentRole.REVISION:
            output = self._revision_output(task, workspace)
        elif role == AgentRole.VERIFICATION:
            output = self._verification_output(task)
        else:
            raise AssertionError(f"unexpected role: {role}")
        return self._result(role, ordinal, "completed", output)

    async def resume_agent(self, thread_id, task, role, workspace, schema, session_dir):
        self.resume_calls.append(role)
        assert session_dir == self.session_dirs[role]
        self.session_dir_calls.append((role, session_dir))
        if role == AgentRole.COPYEDIT:
            output = {"summary": "No copyediting findings.", "findings": []}
        elif role == AgentRole.SUBSTANTIVE_REVIEW:
            output = self._review_output(role, workspace)
        elif role == AgentRole.REVISION:
            output = self._revision_output(task, workspace)
        else:
            raise AssertionError(f"unexpected resumed role: {role}")
        return self._result(role, 2, "completed", output)

    @staticmethod
    def _review_output(role, workspace):
        if role == AgentRole.COPYEDIT:
            return {"summary": "No copyediting findings.", "findings": []}
        source = workspace / "sources" / "main.tex"
        return {
            "summary": "One substantive finding.",
            "findings": [
                {
                    "category": "clarity",
                    "severity": "major",
                    "title": "Typo obscures the claim",
                    "claim": "The main result sentence contains a typo.",
                    "evidence": [
                        {
                            "source_path": "main.tex",
                            "start_line": 3,
                            "end_line": 3,
                            "source_digest": sha256(source.read_bytes()).hexdigest(),
                            "page": 1,
                            "quoted_text": "The result is teh clear.",
                        }
                    ],
                    "explanation": "The typo makes the result sentence harder to read.",
                    "suggested_action": "Replace the sentence with the corrected wording.",
                    "confidence": 0.99,
                }
            ],
        }

    @staticmethod
    def _revision_output(task, workspace):
        finding_ids = re.findall(r'"id": "(finding_[^"]+)"', task)
        source = workspace / "sources" / "main.tex"
        replacement = (
            "The result is clear and precise." if "Human rejection feedback:" in task else "The result is clear."
        )
        return {
            "summary": "Correct the result sentence.",
            "edits": [
                {
                    "finding_ids": finding_ids,
                    "path": "main.tex",
                    "source_digest": sha256(source.read_bytes()).hexdigest(),
                    "start_line": 3,
                    "end_line": 3,
                    "before": "The result is teh clear.",
                    "after": replacement,
                    "rationale": "This directly resolves the confirmed clarity finding.",
                }
            ],
        }

    @staticmethod
    def _verification_output(task):
        finding_ids = re.findall(r'"id": "(finding_[^"]+)"', task)
        return {
            "verdict": "pass",
            "summary": "The approved edit resolves the finding without regression.",
            "resolved_finding_ids": finding_ids,
            "issues": [],
        }

    def _result(self, role, ordinal, status, output):
        return AgentResult(
            thread_id=f"thread-{role.value}-{ordinal}",
            status=status,
            final_response=json.dumps(output) if output is not None else None,
            usage=AgentUsage(input_tokens=100, output_tokens=20),
            trace_jsonl=json.dumps({"role": role.value, "status": status}) + "\n",
            runtime_name=self.runtime_name,
            runtime_version=self.runtime_version,
            model=self.model,
            model_provider=self.provider,
            duration_ms=5,
            error=None if status == "completed" else "simulated interruption",
        )


class ConcurrentFakeAgentRuntime(FakeAgentRuntime):
    def __init__(self):
        super().__init__()
        self.active = 0
        self.max_active = 0

    async def run_agent(self, task, role, workspace, schema, session_dir):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        try:
            return await super().run_agent(task, role, workspace, schema, session_dir)
        finally:
            self.active -= 1


class FailingVerifierRuntime(FakeAgentRuntime):
    async def run_agent(self, task, role, workspace, schema, session_dir):
        if role != AgentRole.VERIFICATION:
            return await super().run_agent(task, role, workspace, schema, session_dir)
        self.run_calls[role] += 1
        output = {
            "verdict": "fail",
            "summary": "The patch needs another human-directed revision.",
            "resolved_finding_ids": [],
            "issues": [
                {
                    "title": "Finding remains unresolved",
                    "explanation": "The proposed wording does not fully resolve the finding.",
                    "evidence": [],
                }
            ],
        }
        return self._result(role, self.run_calls[role], "completed", output)


def make_repository(tmp_path):
    repo = tmp_path / "paper"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Scriptorium Test")
    _git(repo, "config", "user.email", "scriptorium@example.test")
    (repo / "main.tex").write_text(MANUSCRIPT, encoding="utf-8")
    (repo / "scriptorium.toml").write_text(
        (
            "[manuscript]\n"
            'main = "main.tex"\n'
            'engine = "pdflatex"\n\n'
            "[profiles.quick]\n"
            'roles = ["substantive_review", "copyedit"]\n'
        ),
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(".scriptorium/\n", encoding="utf-8")
    _git(repo, "add", "main.tex", "scriptorium.toml", ".gitignore")
    _git(repo, "commit", "-q", "-m", "Initial manuscript")
    state = repo / ".scriptorium"
    state.mkdir()
    (state / "config.toml").write_text(
        (
            "max_concurrency = 2\n\n"
            "[roles]\n"
            'substantive_review = "primary"\n'
            'copyedit = "primary"\n'
            'revision = "primary"\n'
            'verification = "primary"\n\n'
            "[routes.primary]\n"
            'model_provider = "ollama"\n'
            'model = "fake-model"\n'
            "input_usd_per_million = 0\n"
            "output_usd_per_million = 0\n"
            'reasoning_effort = "high"\n'
        ),
        encoding="utf-8",
    )
    return repo


def _git(repo, *arguments):
    subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True)


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
        assert repo.joinpath("main.tex").read_text(encoding="utf-8") == MANUSCRIPT

        patch_id = revised["patch_ids"][0]
        patch_view = service.get_patch(patch_id)
        assert "-The result is teh clear." in patch_view["diff"]
        assert "+The result is clear." in patch_view["diff"]
        service.decide_patch(patch_id, "approve", "The exact replacement is correct.")

        verified = asyncio.run(service.resume_run(run_id))
        assert verified["run"].status == RunStatus.READY_TO_APPLY
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


def test_resume_reuses_completed_review_and_appends_attempt_for_interrupted_lane(tmp_path):
    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime(interrupt_copyedit_once=True)
    manager = PdfBuildingManuscriptManager(repo)

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=manager,
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        run_id = started["run"].id
        assert started["run"].status == RunStatus.REVIEWING
        tasks = {item["task"].role: item["task"] for item in started["tasks"]}
        assert tasks[AgentRole.SUBSTANTIVE_REVIEW].status == TaskStatus.COMPLETED
        assert tasks[AgentRole.COPYEDIT].status == TaskStatus.INTERRUPTED

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=manager,
    ) as service:
        resumed = asyncio.run(service.resume_run(run_id))
        assert resumed["run"].status == RunStatus.AWAITING_DECISION
        tasks = {item["task"].role: item for item in resumed["tasks"]}
        assert len(tasks[AgentRole.SUBSTANTIVE_REVIEW]["attempts"]) == 1
        assert [attempt.status for attempt in tasks[AgentRole.COPYEDIT]["attempts"]] == [
            AttemptStatus.INTERRUPTED,
            AttemptStatus.COMPLETED,
        ]

    assert runtime.run_calls == Counter(
        {
            AgentRole.SUBSTANTIVE_REVIEW: 1,
            AgentRole.COPYEDIT: 1,
        }
    )
    assert runtime.resume_calls == [AgentRole.COPYEDIT]


def test_retry_on_a_different_route_starts_a_new_session(tmp_path):
    repo = make_repository(tmp_path)
    local_path = repo / ".scriptorium" / "config.toml"
    local_path.write_text(
        local_path.read_text(encoding="utf-8")
        + (
            "\n[routes.alternate]\n"
            'runtime = "codex"\n'
            'model_provider = "ollama"\n'
            'model = "fake-model"\n'
            "input_usd_per_million = 0\n"
            "output_usd_per_million = 0\n"
        ),
        encoding="utf-8",
    )
    runtime = FakeAgentRuntime(interrupt_copyedit_once=True)

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        copyedit = next(item["task"] for item in started["tasks"] if item["task"].role == AgentRole.COPYEDIT)

        retried = asyncio.run(service.retry_task(started["run"].id, copyedit.id, "alternate"))

        assert retried["run"].status == RunStatus.AWAITING_DECISION
        assert runtime.run_calls[AgentRole.COPYEDIT] == 2
        assert AgentRole.COPYEDIT not in runtime.resume_calls
        copyedit_session_dirs = [
            session_dir for role, session_dir in runtime.session_dir_calls if role == AgentRole.COPYEDIT
        ]
        assert len(set(copyedit_session_dirs)) == 2
        copyedit_tasks = [item["task"] for item in retried["tasks"] if item["task"].role == AgentRole.COPYEDIT]
        assert {task.route for task in copyedit_tasks} == {"primary", "alternate"}


def test_invalid_anchor_gets_one_persisted_thread_correction(tmp_path):
    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime(invalid_substantive_once=True)

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        substantive = next(item for item in started["tasks"] if item["task"].role == AgentRole.SUBSTANTIVE_REVIEW)

        assert started["run"].status == RunStatus.AWAITING_DECISION
        assert [attempt.status for attempt in substantive["attempts"]] == [
            AttemptStatus.FAILED,
            AttemptStatus.COMPLETED,
        ]
        assert substantive["attempts"][0].output_artifact_digest
        assert substantive["attempts"][1].output_artifact_digest
        assert runtime.resume_calls == [AgentRole.SUBSTANTIVE_REVIEW]


def test_runtime_provenance_mismatch_is_not_accepted_as_a_completed_attempt(tmp_path):
    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime(
        runtime_name="claude_code",
        runtime_version="0.2.128",
        model="fake-model",
        provider="ollama",
    )

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        assert started["run"].status == RunStatus.REVIEWING
        attempts = [attempt for item in started["tasks"] for attempt in item["attempts"]]
        assert attempts
        assert all(attempt.status == AttemptStatus.FAILED for attempt in attempts)
        assert all(attempt.error.startswith("runtime provenance mismatch:") for attempt in attempts)


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


def test_rejected_patch_resumes_the_generating_attempt(tmp_path):
    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        run_id = started["run"].id
        service.decide_finding(
            service.list_findings(run_id)[0].id,
            "confirm",
            "The typo should be corrected.",
        )
        revised = asyncio.run(service.resume_run(run_id))
        first_patch = service.database.get_patch(revised["patch_ids"][0])
        assert first_patch.attempt_id is not None
        generating_attempt = service.database.get_attempt(first_patch.attempt_id)
        generating_task = service.database.get_task(generating_attempt.task_id)
        assert generating_task.route == "primary"

        service.decide_patch(first_patch.id, "reject", "Make the replacement more precise.")
        second_revision = asyncio.run(service.resume_run(run_id))

        assert second_revision["run"].status == RunStatus.AWAITING_PATCH_APPROVAL
        assert runtime.resume_calls == [AgentRole.REVISION]
        assert len(second_revision["patch_ids"]) == 2
        second_patch = service.database.get_patch(second_revision["patch_ids"][-1])
        assert second_patch.attempt_id is not None
        assert second_patch.attempt_id != first_patch.attempt_id


def test_rejected_patch_keeps_its_nondefault_runtime_route(tmp_path):
    repo = make_repository(tmp_path)
    local_path = repo / ".scriptorium" / "config.toml"
    local_path.write_text(
        local_path.read_text(encoding="utf-8")
        + (
            "\n[routes.claude_revision]\n"
            'runtime = "claude_code"\n'
            'model_provider = "anthropic"\n'
            'model = "claude-test"\n'
            "input_usd_per_million = 0\n"
            "output_usd_per_million = 0\n"
        ),
        encoding="utf-8",
    )
    runtimes = {}

    def runtime_factory(route):
        if route.name not in runtimes:
            runtimes[route.name] = FakeAgentRuntime(
                runtime_name=route.runtime,
                runtime_version=route.runtime_version,
                model=route.model,
                provider=route.model_provider,
            )
        return runtimes[route.name]

    with ScriptoriumService(
        repo,
        runtime_factory=runtime_factory,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        run_id = started["run"].id
        service.decide_finding(
            service.list_findings(run_id)[0].id,
            "confirm",
            "The typo should be corrected.",
        )
        service.database.update_run(run_id, RunStatus.REVISING)
        asyncio.run(
            service.armarius._run_revision(
                service.database.get_run(run_id),
                route_override="claude_revision",
            )
        )
        first_patch = service.database.list_patches(run_id)[0]
        generating_attempt = service.database.get_attempt(first_patch.attempt_id)
        assert generating_attempt.runtime_name == "claude_code"
        assert started["run"].frozen_config["role_routes"]["revision"] == "primary"

        service.decide_patch(first_patch.id, "reject", "Make the replacement more precise.")
        asyncio.run(service.resume_run(run_id))

        assert runtimes["claude_revision"].resume_calls == [AgentRole.REVISION]
        assert runtimes["primary"].run_calls[AgentRole.REVISION] == 0


def test_legacy_patch_without_attempt_starts_a_fresh_revision_session(tmp_path):
    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        run_id = started["run"].id
        service.decide_finding(
            service.list_findings(run_id)[0].id,
            "confirm",
            "The typo should be corrected.",
        )
        revised = asyncio.run(service.resume_run(run_id))
        patch_id = revised["patch_ids"][0]
        service.database.connection.execute(
            "UPDATE patches SET attempt_id = NULL WHERE id = ?",
            (patch_id,),
        )

        service.decide_patch(patch_id, "reject", "Make the replacement more precise.")
        asyncio.run(service.resume_run(run_id))

        assert runtime.run_calls[AgentRole.REVISION] == 2
        assert AgentRole.REVISION not in runtime.resume_calls


def test_failed_verification_returns_to_human_gate_without_revision_loop(tmp_path):
    repo = make_repository(tmp_path)
    runtime = FailingVerifierRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        run_id = started["run"].id
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
        assert runtime.run_calls[AgentRole.REVISION] == 1
        assert runtime.run_calls[AgentRole.VERIFICATION] == 1
        assert service.get_patch(patch_id)["verifications"][0].result.value == "fail"
        assert repo.joinpath("main.tex").read_text(encoding="utf-8") == MANUSCRIPT


def test_budget_gate_pauses_before_starting_a_paid_task(tmp_path):
    repo = make_repository(tmp_path)
    local_config = repo / ".scriptorium" / "config.toml"
    local_config.write_text(
        local_config.read_text(encoding="utf-8")
        .replace('model_provider = "ollama"', 'model_provider = "openai"')
        .replace("input_usd_per_million = 0", "input_usd_per_million = 1")
        .replace("output_usd_per_million = 0", "output_usd_per_million = 1"),
        encoding="utf-8",
    )
    runtime = FakeAgentRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", 0))

        assert started["run"].status == RunStatus.WAITING_BUDGET
        assert len(started["tasks"]) == 2
        assert all(not item["attempts"] for item in started["tasks"])
        assert runtime.run_calls == Counter()


def test_mixed_native_routes_freeze_and_record_per_route_runtime(tmp_path):
    repo = make_repository(tmp_path)
    local_path = repo / ".scriptorium" / "config.toml"
    local_path.write_text(
        local_path.read_text(encoding="utf-8")
        .replace('substantive_review = "primary"', 'substantive_review = "claude"')
        .replace('copyedit = "primary"', 'copyedit = "gemini"')
        + (
            "\n[routes.claude]\n"
            'runtime = "claude_code"\n'
            'model_provider = "anthropic"\n'
            'model = "claude-test"\n'
            "input_usd_per_million = 0\n"
            "output_usd_per_million = 0\n"
            "\n[routes.gemini]\n"
            'runtime = "antigravity"\n'
            'model_provider = "gemini"\n'
            'model = "gemini-test"\n'
            "input_usd_per_million = 0\n"
            "output_usd_per_million = 0\n"
        ),
        encoding="utf-8",
    )
    runtimes = {}

    def runtime_factory(route):
        runtime = FakeAgentRuntime(
            runtime_name=route.runtime,
            runtime_version=route.runtime_version,
            model=route.model,
            provider=route.model_provider,
        )
        runtimes[route.name] = runtime
        return runtime

    with ScriptoriumService(
        repo,
        runtime_factory=runtime_factory,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        assert started["run"].status == RunStatus.AWAITING_DECISION
        routes = started["run"].frozen_config["local"]["routes"]
        assert (routes["primary"]["runtime"], routes["primary"]["runtime_version"]) == (
            "codex",
            "0.144.4",
        )
        assert (routes["claude"]["runtime"], routes["claude"]["runtime_version"]) == (
            "claude_code",
            "0.2.128",
        )
        assert (routes["gemini"]["runtime"], routes["gemini"]["runtime_version"]) == (
            "antigravity",
            "0.1.8",
        )
        attempts = [attempt for item in started["tasks"] for attempt in item["attempts"]]
        assert {attempt.runtime_name for attempt in attempts} == {"claude_code", "antigravity"}
        assert set(runtimes) == {"claude", "gemini"}


def test_legacy_frozen_run_uses_its_top_level_codex_runtime(tmp_path):
    repo = make_repository(tmp_path)

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: FakeAgentRuntime(),
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        frozen = json.loads(json.dumps(started["run"].frozen_config))
        for route in frozen["local"]["routes"].values():
            route.pop("runtime")
            route.pop("runtime_version")
        frozen["runtime"] = {"name": "codex", "version": "0.144.4"}
        legacy_run = replace(started["run"], frozen_config=frozen)

        route = service.armarius._route_for_run(
            legacy_run,
            AgentRole.SUBSTANTIVE_REVIEW,
        )

        assert route.runtime == "codex"
        assert route.runtime_version == "0.144.4"


@pytest.mark.parametrize(("limit", "expected_max_active"), [(1, 1), (2, 2)])
def test_review_concurrency_respects_frozen_limit(tmp_path, limit, expected_max_active):
    repo = make_repository(tmp_path)
    local_config = repo / ".scriptorium" / "config.toml"
    local_config.write_text(
        local_config.read_text(encoding="utf-8").replace("max_concurrency = 2", f"max_concurrency = {limit}"),
        encoding="utf-8",
    )
    runtime = ConcurrentFakeAgentRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

    assert started["run"].status == RunStatus.AWAITING_DECISION
    assert runtime.max_active == expected_max_active
