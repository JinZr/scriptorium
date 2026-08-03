import asyncio

import pytest

from scriptorium.domain import RunStatus
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
