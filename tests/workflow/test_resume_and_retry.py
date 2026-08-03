import asyncio
from collections import Counter
import json
from pathlib import Path

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus, TaskStatus
from scriptorium.errors import InfrastructureError
from scriptorium.runtime import AgentCancelled
from scriptorium.service import ScriptoriumService
from scriptorium.workflow import Armarius

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


class InfrastructureFailingRuntime(FakeAgentRuntime):
    async def run_agent(self, task, role, workspace, schema, session_dir, on_session_started=None):
        if role == AgentRole.COPYEDIT:
            raise InfrastructureError("runtime worker failed: authentication failed")
        return await super().run_agent(task, role, workspace, schema, session_dir, on_session_started)


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
        assert attempt.validation_report_artifact_digest is None


def test_runtime_infrastructure_failure_durably_fails_the_attempt(tmp_path):
    repo = make_repository(tmp_path)
    runtime = InfrastructureFailingRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        with pytest.raises(InfrastructureError, match="authentication failed"):
            asyncio.run(service.start_run("HEAD", "quick", None))

        run = service.database.list_runs()[0]
        copyedit = next(task for task in service.database.list_tasks(run.id) if task.role == AgentRole.COPYEDIT)
        attempt = service.database.list_attempts(copyedit.id)[0]

        assert run.status == RunStatus.FAILED
        assert copyedit.status == TaskStatus.FAILED
        assert attempt.status == AttemptStatus.FAILED
        assert attempt.error == "runtime worker failed: authentication failed"
        assert attempt.completed_at is not None
        assert attempt.estimated_cost_usd == 0
        assert attempt.trace_artifact_digest is not None
        assert attempt.validation_report_artifact_digest is None
        events = [
            event
            for event in service.database.list_events(run.id)
            if event.event_type == "attempt.finished" and event.entity_id == attempt.id
        ]
        assert len(events) == 1
        assert events[0].payload["status"] == AttemptStatus.FAILED.value


def test_resume_rebuilds_verification_bundle_after_materialization_failure(tmp_path, monkeypatch):
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
        service.decide_finding(
            service.list_findings(run_id)[0].id,
            "confirm",
            "The typo should be corrected.",
        )
        revised = asyncio.run(service.resume_run(run_id))
        patch_id = revised["patch_ids"][0]
        service.decide_patch(patch_id, "approve", "Verify this exact edit.")
        verification_workspace = repo / ".scriptorium" / "runs" / run_id / "verifications" / patch_id / "bundle"
        original_write_text = Path.write_text

        def fail_source_map_write(path, data, *args, **kwargs):
            if "verifications" in path.parts and path.name in {"source-map.json", ".source-map.json.tmp"}:
                original_write_text(path, "{", *args, **kwargs)
                raise InfrastructureError("simulated bundle metadata write failure")
            return original_write_text(path, data, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "write_text", fail_source_map_write)
            with pytest.raises(InfrastructureError, match="simulated bundle metadata write failure"):
                asyncio.run(service.resume_run(run_id))

        assert service.database.get_run(run_id).status == RunStatus.FAILED
        assert (verification_workspace / "manifest.json").is_file()
        assert not (verification_workspace / "source-map.json").exists()

        resumed = asyncio.run(service.resume_run(run_id))

        assert resumed["run"].status == RunStatus.READY_TO_APPLY
        assert runtime.run_calls[AgentRole.VERIFICATION] == 1


def test_resume_reuses_completed_review_and_appends_attempt_for_interrupted_lane(tmp_path, monkeypatch):
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
        copyedit_task = tasks[AgentRole.COPYEDIT]
        copyedit_attempt = next(
            item["attempts"][0] for item in started["tasks"] if item["task"].role == AgentRole.COPYEDIT
        )
        frozen_prompt = runtime.tasks[AgentRole.COPYEDIT]

    monkeypatch.setattr(
        Armarius,
        "_review_prompt_template",
        staticmethod(lambda role_prompt, contract: "changed current renderer"),
    )

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
        assert tasks[AgentRole.COPYEDIT]["task"].id == copyedit_task.id
        assert tasks[AgentRole.COPYEDIT]["task"].input_digest == copyedit_task.input_digest
        assert {attempt.thread_id for attempt in tasks[AgentRole.COPYEDIT]["attempts"]} == {copyedit_attempt.thread_id}
        assert runtime.tasks[AgentRole.COPYEDIT] == frozen_prompt

    assert runtime.run_calls == Counter(
        {
            AgentRole.SUBSTANTIVE_REVIEW: 1,
            AgentRole.COPYEDIT: 1,
        }
    )
    assert runtime.resume_calls == [AgentRole.COPYEDIT]


def test_resume_rejects_corrupt_frozen_anchor_contract_before_runtime(tmp_path):
    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime(interrupt_copyedit_once=True)

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        run_id = started["run"].id
        frozen_config = json.loads(json.dumps(started["run"].frozen_config))
        frozen_config["evidence_anchor_contract"]["digest"] = "0" * 64
        service.database.connection.execute(
            "UPDATE runs SET frozen_config_json = ? WHERE id = ?",
            (json.dumps(frozen_config), run_id),
        )
        before = {item["task"].id: [attempt.status for attempt in item["attempts"]] for item in started["tasks"]}

        with pytest.raises(InfrastructureError, match="corrupt frozen evidence anchor contract"):
            asyncio.run(service.resume_run(run_id))

        after = service.get_run(run_id)
        assert {item["task"].id: [attempt.status for attempt in item["attempts"]] for item in after["tasks"]} == before
        assert runtime.resume_calls == []


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
