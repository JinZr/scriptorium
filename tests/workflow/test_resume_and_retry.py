import asyncio
from collections import Counter

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus, TaskStatus
from scriptorium.runtime import AgentCancelled
from scriptorium.service import ScriptoriumService

from ._support import FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository


class CancellingRuntime(FakeAgentRuntime):
    async def run_agent(self, task, role, workspace, schema, session_dir, on_session_started=None):
        if role != AgentRole.COPYEDIT:
            return await super().run_agent(
                task,
                role,
                workspace,
                schema,
                session_dir,
                on_session_started,
            )
        if on_session_started is not None:
            on_session_started("thread-copyedit-1")
        result = self._result(role, 1, "interrupted", None)
        raise AgentCancelled(result)


def test_runtime_cancellation_durably_interrupts_the_attempt(tmp_path):
    repo = make_repository(tmp_path)
    runtime = CancellingRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(service.start_run("HEAD", "quick", None))

        run = service.database.list_runs()[0]
        copyedit = next(task for task in service.database.list_tasks(run.id) if task.role == AgentRole.COPYEDIT)
        attempt = service.database.list_attempts(copyedit.id)[0]

        assert run.status == RunStatus.REVIEWING
        assert copyedit.status == TaskStatus.INTERRUPTED
        assert attempt.status == AttemptStatus.INTERRUPTED
        assert attempt.thread_id == "thread-copyedit-1"


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
