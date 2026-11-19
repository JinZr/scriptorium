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


# Builds in order: the frozen base (1), the writing submission's own compile check (2, which only needs to compile),
# and the combined candidate (3), whose build also gives the patched bundle the check reads.
@pytest.mark.parametrize("fail_at", [1, 3, None])
def test_compiler_coverage_gates_each_external_build_stage(tmp_path, fail_at):
    repo = make_repository(tmp_path)
    manager = RecordedBuildManager(repo, fail_at)
    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        if fail_at == 1:
            with pytest.raises(InfrastructureError, match="hidden.tex"):
                asyncio.run(service.start_run("HEAD", "quick"))
            run = service.store.list_runs()[0]
            assert run.status == RunStatus.FAILED
            assert service.store.list_tasks(run.id) == []
            assert service.list_tasks(run.id)["next_actions"] == [{"command": "run resume", "run_id": run.id}]
            manager.fail_at = None
            assert asyncio.run(service.resume_run(run.id))["run"].status == RunStatus.REVIEWING
            return
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        if fail_at == 3:
            with pytest.raises(InfrastructureError, match="hidden.tex"):
                prepare_patch(service, run.id)
            assert service.store.list_patches(run.id) == []
            assert service.get_run(run.id)["run"].status == RunStatus.FAILED
            assert service.list_tasks(run.id)["next_actions"] == [{"command": "run resume", "run_id": run.id}]
            manager.fail_at = None
            assert asyncio.run(service.resume_run(run.id))["run"].status == RunStatus.CHECKING
        else:
            prepare_patch(service, run.id)
            assert service.store.get_run(run.id).status == RunStatus.CHECKING
        # A recovered candidate builds again; the check needs no build of its own.
        assert manager.builds == (4 if fail_at == 3 else 3)
        assert (repo / "main.tex").read_text() == MANUSCRIPT
