from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from scriptorium.domain import AgentRole
from scriptorium.runtime import CLAUDE_SDK_VERSION, AgentResult, AgentUsage

from ._fake_sdk import FakeAssistantMessage, FakeQuery, _runtime, _success


def test_run_agent_uses_isolated_read_only_options_and_normalizes_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = FakeQuery([[FakeAssistantMessage("working"), _success()]])
    runtime = _runtime(query)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_dir = tmp_path / "session"
    ambient_config_dir = tmp_path / "ambient-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ambient_config_dir))
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}, "z": {"type": "integer"}},
        "required": ["answer", "z"],
    }

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
        thread_id="11111111-1111-4111-8111-111111111111",
        status="completed",
        final_response='{"answer":"ok","z":2}',
        usage=AgentUsage(
            input_tokens=18,
            cached_input_tokens=5,
            output_tokens=7,
            reasoning_tokens=0,
        ),
        trace_jsonl=result.trace_jsonl,
        runtime_name="claude_code",
        runtime_version=CLAUDE_SDK_VERSION,
        model="claude-test",
        model_provider="anthropic",
        duration_ms=123,
        error=None,
    )

    assert len(query.calls) == 1
    prompt, options = query.calls[0]
    assert prompt == "Review the manuscript."
    assert options.tools == ["Glob", "Grep", "Read"]
    assert options.allowed_tools == []
    assert options.permission_mode == "dontAsk"
    assert options.cwd == workspace.resolve()
    assert options.resume is None
    assert options.model == "claude-test"
    assert options.fallback_model is None
    assert options.effort == "high"
    assert options.output_format == {"type": "json_schema", "schema": schema}
    native_config_dir = Path(options.env["CLAUDE_CONFIG_DIR"])
    assert native_config_dir != ambient_config_dir
    assert native_config_dir.name.startswith("scriptorium-claude-")
    assert not native_config_dir.is_relative_to(session_dir.resolve())
    assert query.native_configs_ready == [True]
    assert not native_config_dir.exists()
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    assert options.settings is None
    assert options.add_dirs == []
    assert options.setting_sources == []
    assert options.skills == []
    assert options.plugins == []
    assert options.agents is None
    assert options.session_store_flush == "eager"
    assert options.hooks.keys() == {"PreToolUse"}
    assert options.hooks["PreToolUse"][0].matcher is None
    assert "Scholiast" in options.system_prompt
    assert "Work read-only" in options.system_prompt
    assert list(session_dir.glob("*.jsonl"))

    trace = [json.loads(line) for line in result.trace_jsonl.splitlines()]
    assert trace[0] == {
        "kind": "message",
        "message": {"session_id": None, "text": "working"},
    }
    assert trace[1]["message"]["usage"]["cache_creation_input_tokens"] == 3
    assert trace[1]["message"]["total_cost_usd"] == 0.012
    assert trace[-1] == {
        "kind": "normalized_result",
        "status": "completed",
        "usage": {
            "cached_input_tokens": 5,
            "input_tokens": 18,
            "output_tokens": 7,
            "reasoning_tokens": 0,
        },
    }
