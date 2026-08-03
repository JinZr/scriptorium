import asyncio
from collections import Counter

from scriptorium.domain import AgentRole, RunStatus
from scriptorium.service import ScriptoriumService

from ._support import MANUSCRIPT, FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository


class FailingVerifierRuntime(FakeAgentRuntime):
    async def run_agent(self, task, role, workspace, schema, session_dir, on_session_started=None):
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
