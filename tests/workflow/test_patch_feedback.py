import asyncio

from scriptorium.domain import AgentRole, RunStatus
from scriptorium.service import ScriptoriumService

from ._support import FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository


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
