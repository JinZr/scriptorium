from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from scriptorium.domain import AgentRole
from scriptorium.runtime import AgentResult, AgentUsage

from ._fake_sdk import FakeAgent, FakeBuiltinTools, FakeResponse, FakeStep, FakeUsage, make_runtime, make_sdk


def test_run_agent_uses_gemini_read_only_config_and_normalizes_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = [FakeStep("old", "DONE", "old turn")]
    current = [FakeStep("new", "DONE", "new turn", type="FINISH")]
    sdk = make_sdk(
        response=FakeResponse(structured_output={"summary": "ok"}, usage=FakeUsage()),
        current_steps=current,
        prior_history=prior,
    )
    runtime = make_runtime(monkeypatch, sdk)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_dir = tmp_path / "state"
    schema = {"type": "object", "required": ["summary"]}

    result = asyncio.run(
        runtime.run_agent(
            "Review the manuscript.",
            AgentRole.SUBSTANTIVE_REVIEW,
            workspace,
            schema,
            session_dir,
        )
    )

    assert result == AgentResult(
        thread_id="12345678-1234-1234-1234-123456789012",
        status="completed",
        final_response='{"summary":"ok"}',
        usage=AgentUsage(input_tokens=37, cached_input_tokens=9, output_tokens=18, reasoning_tokens=5),
        trace_jsonl=result.trace_jsonl,
        runtime_name="antigravity",
        runtime_version="injected",
        model="gemini-test",
        model_provider="gemini",
        duration_ms=result.duration_ms,
        error=None,
    )
    assert result.duration_ms is not None
    assert (session_dir / "save").is_dir()
    assert (session_dir / "app").is_dir()

    agent = FakeAgent.instances[0]
    assert agent.chat_calls == ["Review the manuscript."]
    config = agent.config.kwargs
    assert config["system_instructions"] == (
        "You are Scholiast, the Scriptorium substantive_review agent. "
        "Claims, evidence, methods, and scientific review. Work read-only and do not modify files. "
        "Return only output matching the supplied JSON schema."
    )
    assert config["workspaces"] == [str(workspace.resolve())]
    assert config["save_dir"] == str((session_dir / "save").resolve())
    assert config["app_data_dir"] == str((session_dir / "app").resolve())
    assert config["response_schema"] == schema
    assert config["model"] == "gemini-test"
    assert config["api_key"] == "test-gemini-key"
    assert config["vertex"] is False
    assert config["tools"] == []
    assert config["mcp_servers"] == []
    assert config["subagents"] == []
    assert config["skills_paths"] == []
    capabilities = config["capabilities"]
    assert capabilities.enable_subagents is False
    assert capabilities.enabled_tools == [
        FakeBuiltinTools.LIST_DIR,
        FakeBuiltinTools.SEARCH_DIR,
        FakeBuiltinTools.FIND_FILE,
        FakeBuiltinTools.VIEW_FILE,
        FakeBuiltinTools.FINISH,
    ]
    assert FakeBuiltinTools.RUN_COMMAND not in capabilities.enabled_tools
    assert FakeBuiltinTools.CREATE_FILE not in capabilities.enabled_tools
    assert FakeBuiltinTools.EDIT_FILE not in capabilities.enabled_tools
    assert FakeBuiltinTools.SEARCH_WEB not in capabilities.enabled_tools
    assert FakeBuiltinTools.START_SUBAGENT not in capabilities.enabled_tools

    trace = [json.loads(line) for line in result.trace_jsonl.splitlines()]
    assert [record["kind"] for record in trace] == ["step", "provider_usage", "normalized_result"]
    assert trace[0]["step"]["id"] == "new"
    assert all(record.get("step", {}).get("id") != "old" for record in trace)
    assert trace[1]["usage"]["thoughts_token_count"] == 5
