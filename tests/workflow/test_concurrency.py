import asyncio

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus, TaskStatus
from scriptorium.errors import InfrastructureError
from scriptorium.service import ScriptoriumService

from ._support import FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository


class ConcurrentFakeAgentRuntime(FakeAgentRuntime):
    def __init__(self):
        super().__init__()
        self.active = 0
        self.max_active = 0

    async def run_agent(self, task, role, workspace, schema, session_dir, on_session_started=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        try:
            return await super().run_agent(task, role, workspace, schema, session_dir)
        finally:
            self.active -= 1


class FailingConcurrentFakeAgentRuntime(FakeAgentRuntime):
    def __init__(self):
        super().__init__()
        self.copyedit_started = asyncio.Event()
        self.copyedit_cancelled = False

    async def run_agent(self, task, role, workspace, schema, session_dir, on_session_started=None):
        if on_session_started is not None:
            on_session_started(f"thread-{role.value}-1")
        if role == AgentRole.SUBSTANTIVE_REVIEW:
            await self.copyedit_started.wait()
            raise InfrastructureError("runtime worker failed")
        self.copyedit_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.copyedit_cancelled = True
            raise


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


def test_review_failure_cancels_and_drains_running_siblings_before_returning(tmp_path):
    repo = make_repository(tmp_path)
    runtime = FailingConcurrentFakeAgentRuntime()

    async def exercise():
        with ScriptoriumService(
            repo,
            runtime_factory=lambda route: runtime,
            manuscript_manager=PdfBuildingManuscriptManager(repo),
        ) as service:
            with pytest.raises(InfrastructureError, match="runtime worker failed"):
                await service.start_run("HEAD", "quick", None)

            run = service.database.list_runs()[0]
            tasks = {task.role: task for task in service.database.list_tasks(run.id)}
            copyedit_attempt = service.database.list_attempts(tasks[AgentRole.COPYEDIT].id)[0]

            assert runtime.copyedit_cancelled
            assert run.status == RunStatus.FAILED
            assert tasks[AgentRole.SUBSTANTIVE_REVIEW].status == TaskStatus.FAILED
            assert tasks[AgentRole.COPYEDIT].status == TaskStatus.INTERRUPTED
            assert copyedit_attempt.status == AttemptStatus.INTERRUPTED
            assert service.database.list_findings(run.id) == []

    asyncio.run(exercise())
