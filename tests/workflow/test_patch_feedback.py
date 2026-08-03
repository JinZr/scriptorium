import asyncio
from dataclasses import replace

import pytest

from scriptorium.domain import AgentRole, PatchStatus, RunStatus
from scriptorium.service import ScriptoriumService

from ._support import FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository


class RecordingRevisionRuntime(FakeAgentRuntime):
    def __init__(self, *, correction_status_once=None, **kwargs):
        super().__init__(**kwargs)
        self.correction_status_once = correction_status_once
        self.revision_resume_prompts = []
        self.revision_resume_thread_ids = []

    async def resume_agent(
        self,
        thread_id,
        task,
        role,
        workspace,
        schema,
        session_dir,
        on_session_started=None,
    ):
        if role != AgentRole.REVISION:
            return await super().resume_agent(thread_id, task, role, workspace, schema, session_dir)
        self.revision_resume_prompts.append(task)
        self.revision_resume_thread_ids.append(thread_id)
        if self.correction_status_once is not None:
            status = self.correction_status_once
            self.correction_status_once = None
            self.resume_calls.append(role)
            assert session_dir == self.session_dirs[role]
            self.session_dir_calls.append((role, session_dir))
            return replace(self._result(role, 2, status, None), thread_id=thread_id)
        return await super().resume_agent(thread_id, task, role, workspace, schema, session_dir)


class RepeatingRevisionRuntime(FakeAgentRuntime):
    @staticmethod
    def _revision_output(task, workspace):
        output = FakeAgentRuntime._revision_output(task, workspace)
        output["edits"][0]["after"] = "The result is clear."
        return output


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


@pytest.mark.parametrize("terminal_status", ["failed", "interrupted"])
def test_rejected_patch_correction_recovers_context_after_terminal_attempt(tmp_path, terminal_status):
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
    primary = FakeAgentRuntime()
    revision = RecordingRevisionRuntime(
        correction_status_once=terminal_status,
        runtime_name="claude_code",
        runtime_version="0.2.128",
        model="claude-test",
        provider="anthropic",
    )

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: revision if route.name == "claude_revision" else primary,
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
        service.decide_patch(first_patch.id, "reject", "Make the replacement more precise.")

        first_correction = asyncio.run(service.resume_run(run_id))
        assert first_correction["run"].status == RunStatus.REVISING

        second_correction = asyncio.run(service.resume_run(run_id))

        assert second_correction["run"].status == RunStatus.AWAITING_PATCH_APPROVAL
        assert revision.revision_resume_thread_ids == [
            generating_attempt.thread_id,
            generating_attempt.thread_id,
        ]
        assert all(
            "Human rejection feedback:\nMake the replacement more precise." in prompt
            for prompt in revision.revision_resume_prompts
        )
        assert primary.run_calls[AgentRole.REVISION] == 0


def test_rejected_patch_correction_recovers_context_after_budget_wait(tmp_path):
    repo = make_repository(tmp_path)
    local_path = repo / ".scriptorium" / "config.toml"
    local_path.write_text(
        local_path.read_text(encoding="utf-8")
        .replace('model_provider = "ollama"', 'model_provider = "openai"')
        .replace("input_usd_per_million = 0", "input_usd_per_million = 1")
        .replace("output_usd_per_million = 0", "output_usd_per_million = 1"),
        encoding="utf-8",
    )
    runtime = RecordingRevisionRuntime(provider="openai")

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", 1))
        run_id = started["run"].id
        service.decide_finding(
            service.list_findings(run_id)[0].id,
            "confirm",
            "The typo should be corrected.",
        )
        revised = asyncio.run(service.resume_run(run_id))
        first_patch = service.database.get_patch(revised["patch_ids"][0])
        generating_attempt = service.database.get_attempt(first_patch.attempt_id)
        service.decide_patch(first_patch.id, "reject", "Make the replacement more precise.")
        spent = service.database.get_run(run_id).estimated_cost_usd
        service.database.connection.execute(
            "UPDATE runs SET budget_usd = ? WHERE id = ?",
            (spent, run_id),
        )

        waiting = asyncio.run(service.resume_run(run_id))
        assert waiting["run"].status == RunStatus.WAITING_BUDGET
        assert runtime.revision_resume_thread_ids == []

        service.database.connection.execute(
            "UPDATE runs SET budget_usd = ? WHERE id = ?",
            (1, run_id),
        )
        resumed = asyncio.run(service.resume_run(run_id))

        assert resumed["run"].status == RunStatus.AWAITING_PATCH_APPROVAL
        assert runtime.revision_resume_thread_ids == [generating_attempt.thread_id]
        assert "Human rejection feedback:\nMake the replacement more precise." in runtime.revision_resume_prompts[0]


def test_rejected_patch_correction_reproposes_the_same_diff_with_new_lineage(tmp_path):
    repo = make_repository(tmp_path)
    runtime = RepeatingRevisionRuntime()

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
        original_attempt_id = first_patch.attempt_id
        service.decide_patch(first_patch.id, "reject", "Make the replacement more precise.")

        reproposed = asyncio.run(service.resume_run(run_id))

        patches = service.database.list_patches(run_id)
        revision_attempts = [
            attempt
            for task in service.database.list_tasks(run_id)
            if task.stage == "revision"
            for attempt in service.database.list_attempts(task.id)
        ]
        assert reproposed["run"].status == RunStatus.AWAITING_PATCH_APPROVAL
        assert len(patches) == 1
        assert patches[0].id == first_patch.id
        assert patches[0].status == PatchStatus.PROPOSED
        assert len(revision_attempts) == 2
        assert patches[0].attempt_id == revision_attempts[-1].id
        assert patches[0].attempt_id != original_attempt_id
        reproposal_events = [
            event for event in service.database.list_events(run_id) if event.event_type == "patch.reproposed"
        ]
        assert len(reproposal_events) == 1
        assert reproposal_events[0].payload["previous_attempt_id"] == original_attempt_id
        assert reproposal_events[0].payload["attempt_id"] == patches[0].attempt_id

        service.decide_patch(first_patch.id, "reject", "The correction is still unchanged.")
        asyncio.run(service.resume_run(run_id))

        assert runtime.resume_calls[-1] == AgentRole.REVISION
        assert service.database.get_patch(first_patch.id).status == PatchStatus.PROPOSED


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
