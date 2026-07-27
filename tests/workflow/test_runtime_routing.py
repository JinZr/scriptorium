import asyncio
from dataclasses import replace
import json

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus
from scriptorium.service import ScriptoriumService

from ._support import FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository


def test_runtime_provenance_mismatch_is_not_accepted_as_a_completed_attempt(tmp_path):
    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime(
        runtime_name="claude_code",
        runtime_version="0.2.128",
        model="fake-model",
        provider="ollama",
    )

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        assert started["run"].status == RunStatus.REVIEWING
        attempts = [attempt for item in started["tasks"] for attempt in item["attempts"]]
        assert attempts
        assert all(attempt.status == AttemptStatus.FAILED for attempt in attempts)
        assert all(attempt.error.startswith("runtime provenance mismatch:") for attempt in attempts)


def test_mixed_native_routes_freeze_and_record_per_route_runtime(tmp_path):
    repo = make_repository(tmp_path)
    local_path = repo / ".scriptorium" / "config.toml"
    local_path.write_text(
        local_path.read_text(encoding="utf-8")
        .replace('substantive_review = "primary"', 'substantive_review = "claude"')
        .replace('copyedit = "primary"', 'copyedit = "gemini"')
        + (
            "\n[routes.claude]\n"
            'runtime = "claude_code"\n'
            'model_provider = "anthropic"\n'
            'model = "claude-test"\n'
            "input_usd_per_million = 0\n"
            "output_usd_per_million = 0\n"
            "\n[routes.gemini]\n"
            'runtime = "antigravity"\n'
            'model_provider = "gemini"\n'
            'model = "gemini-test"\n'
            "input_usd_per_million = 0\n"
            "output_usd_per_million = 0\n"
        ),
        encoding="utf-8",
    )
    runtimes = {}

    def runtime_factory(route):
        runtime = FakeAgentRuntime(
            runtime_name=route.runtime,
            runtime_version=route.runtime_version,
            model=route.model,
            provider=route.model_provider,
        )
        runtimes[route.name] = runtime
        return runtime

    with ScriptoriumService(
        repo,
        runtime_factory=runtime_factory,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        assert started["run"].status == RunStatus.AWAITING_DECISION
        routes = started["run"].frozen_config["local"]["routes"]
        assert (routes["primary"]["runtime"], routes["primary"]["runtime_version"]) == (
            "codex",
            "0.144.4",
        )
        assert (routes["claude"]["runtime"], routes["claude"]["runtime_version"]) == (
            "claude_code",
            "0.2.128",
        )
        assert (routes["gemini"]["runtime"], routes["gemini"]["runtime_version"]) == (
            "antigravity",
            "0.1.8",
        )
        attempts = [attempt for item in started["tasks"] for attempt in item["attempts"]]
        assert {attempt.runtime_name for attempt in attempts} == {"claude_code", "antigravity"}
        assert set(runtimes) == {"claude", "gemini"}


def test_legacy_frozen_run_uses_its_top_level_codex_runtime(tmp_path):
    repo = make_repository(tmp_path)

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: FakeAgentRuntime(),
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        frozen = json.loads(json.dumps(started["run"].frozen_config))
        for route in frozen["local"]["routes"].values():
            route.pop("runtime")
            route.pop("runtime_version")
        frozen["runtime"] = {"name": "codex", "version": "0.144.4"}
        legacy_run = replace(started["run"], frozen_config=frozen)

        route = service.armarius._route_for_run(
            legacy_run,
            AgentRole.SUBSTANTIVE_REVIEW,
        )

        assert route.runtime == "codex"
        assert route.runtime_version == "0.144.4"
