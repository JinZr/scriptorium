import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest

from scriptorium.domain import RunStatus
from scriptorium.errors import InfrastructureError
from scriptorium.manuscript import CompilerInput
from scriptorium.service import ScriptoriumService

from ._support import MANUSCRIPT, PdfBuildingManuscriptManager, make_repository, prepare_patch


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
def test_compiler_coverage_gates_each_external_build_stage(tmp_path, fail_at):
    repo = make_repository(tmp_path)
    manager = RecordedBuildManager(repo, fail_at)
    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        if fail_at == 1:
            with pytest.raises(InfrastructureError, match="hidden.tex"):
                asyncio.run(service.start_run("HEAD", "quick"))
            run = service.database.list_runs()[0]
            assert run.status == RunStatus.FAILED
            assert service.database.list_tasks(run.id) == []
            return
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        if fail_at == 2:
            with pytest.raises(InfrastructureError, match="hidden.tex"):
                prepare_patch(service, run.id)
            assert service.database.list_patches(run.id) == []
        else:
            patch, _ = prepare_patch(service, run.id)
            service.decide_patch(patch.id, "approve", "Verify the exact edit.")
            if fail_at == 3:
                with pytest.raises(InfrastructureError, match="hidden.tex"):
                    asyncio.run(service.resume_run(run.id))
            else:
                view = asyncio.run(service.resume_run(run.id))
                assert view["run"].status == RunStatus.VERIFYING
        if fail_at:
            assert service.get_run(run.id)["run"].status == RunStatus.FAILED
        assert (repo / "main.tex").read_text() == MANUSCRIPT
