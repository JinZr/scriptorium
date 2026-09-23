import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest

from scriptorium.domain import AgentRole, RunStatus
from scriptorium.errors import InfrastructureError
from scriptorium.manuscript import CompilerInput
from scriptorium.service import ScriptoriumService

from ._support import MANUSCRIPT, FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository


class RecordedBuildManager(PdfBuildingManuscriptManager):
    def __init__(self, repo, fail_at=None):
        super().__init__(repo)
        self.fail_at = fail_at
        self.builds = 0

    def build(self, workspace, manuscript):
        result = super().build(workspace, manuscript)
        self.builds += 1
        inputs = [
            CompilerInput(manuscript.main, sha256((workspace / manuscript.main).read_bytes()).hexdigest(), "review")
        ]
        if self.builds == self.fail_at:
            inputs.append(CompilerInput("hidden.tex", "missing", "review"))
        return replace(result, compiler_inputs=tuple(inputs))


@pytest.mark.parametrize("fail_at", [1, 2, 3, None])
def test_compiler_coverage_gates_each_build_stage(tmp_path, monkeypatch, fail_at):
    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime()
    manager = RecordedBuildManager(repo, fail_at)
    with ScriptoriumService(repo, runtime_factory=lambda route: runtime, manuscript_manager=manager) as service:
        if fail_at == 1:
            with pytest.raises(InfrastructureError, match="hidden.tex"):
                asyncio.run(service.start_run("HEAD", "quick", None))
            run = service.database.list_runs()[0]
            assert run.status == RunStatus.FAILED
            assert not runtime.run_calls
            frozen = run.frozen_config
            manager.fail_at = 2
            monkeypatch.setattr(manager, "scan_sources", lambda *args: pytest.fail("resume must reuse frozen sources"))
            with pytest.raises(InfrastructureError, match="hidden.tex"):
                asyncio.run(service.resume_run(run.id))
            assert service.get_run(run.id)["run"].frozen_config == frozen
            assert not runtime.run_calls
            return
        run = asyncio.run(service.start_run("HEAD", "quick", None))["run"]
        finding = service.list_findings(run.id)[0]
        service.decide_finding(finding.id, "confirm", "Correct the typo.")
        if fail_at == 2:
            with pytest.raises(InfrastructureError, match="hidden.tex"):
                asyncio.run(service.resume_run(run.id))
            assert not service.database.list_patches(run.id)
        else:
            revised = asyncio.run(service.resume_run(run.id))
            service.decide_patch(revised["patch_ids"][0], "approve", "Verify the exact edit.")
            if fail_at == 3:
                with pytest.raises(InfrastructureError, match="hidden.tex"):
                    asyncio.run(service.resume_run(run.id))
            else:
                verified = asyncio.run(service.resume_run(run.id))
                assert verified["run"].status == RunStatus.READY_TO_APPLY
        if fail_at:
            assert service.get_run(run.id)["run"].status == RunStatus.FAILED
            assert runtime.run_calls[AgentRole.VERIFICATION] == 0
        assert (repo / "main.tex").read_text() == MANUSCRIPT
