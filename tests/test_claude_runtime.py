from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import pytest

from scriptorium.claude_runtime import ClaudeCodeAgentRuntime
from scriptorium.domain import AgentRole
from scriptorium.runtime import CLAUDE_SDK_VERSION, AgentResult, AgentUsage, RuntimeUnavailable


@dataclass
class FakeHookMatcher:
    matcher: str | None
    hooks: list[Any]


class FakeOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


@dataclass
class FakeAssistantMessage:
    text: str
    session_id: str | None = None


@dataclass
class FakeResultMessage:
    subtype: str
    duration_ms: int
    is_error: bool
    session_id: str
    usage: dict[str, Any] | None
    structured_output: Any = None
    result: str | None = None
    errors: list[str] | None = None
    terminal_reason: str | None = "completed"
    total_cost_usd: float | None = None


class FakeQuery:
    def __init__(self, responses: list[list[Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, FakeOptions]] = []
        self.loaded_sessions: list[list[dict[str, Any]] | None] = []

    def __call__(self, *, prompt: str, options: FakeOptions):
        self.calls.append((prompt, options))
        messages = self.responses.pop(0)

        async def stream():
            if options.resume is not None:
                self.loaded_sessions.append(
                    await options.session_store.load({"project_key": "test", "session_id": options.resume})
                )
            for index, message in enumerate(messages):
                session_id = getattr(message, "session_id", None)
                if session_id is not None:
                    await options.session_store.append(
                        {"project_key": "test", "session_id": session_id},
                        [
                            {
                                "type": "message",
                                "uuid": f"{session_id}-{len(self.calls)}-{index}",
                            }
                        ],
                    )
                yield message

        return stream()


def _sdk(query: Any, version: str = CLAUDE_SDK_VERSION) -> Any:
    return type(
        "FakeSDK",
        (),
        {
            "__version__": version,
            "query": staticmethod(query),
            "ClaudeAgentOptions": FakeOptions,
            "HookMatcher": FakeHookMatcher,
            "ResultMessage": FakeResultMessage,
        },
    )


def _runtime(query: Any) -> ClaudeCodeAgentRuntime:
    return ClaudeCodeAgentRuntime(
        route="claude_review",
        model="claude-test",
        provider="anthropic",
        reasoning="high",
        sdk_loader=lambda: _sdk(query),
    )


def _success(session_id: str = "11111111-1111-4111-8111-111111111111") -> FakeResultMessage:
    return FakeResultMessage(
        subtype="success",
        duration_ms=123,
        is_error=False,
        session_id=session_id,
        usage={
            "input_tokens": 10,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 5,
            "output_tokens": 7,
            "service_tier": "standard",
        },
        structured_output={"z": 2, "answer": "ok"},
        total_cost_usd=0.012,
    )


def test_run_agent_uses_isolated_read_only_options_and_normalizes_result(tmp_path: Path) -> None:
    query = FakeQuery([[FakeAssistantMessage("working"), _success()]])
    runtime = _runtime(query)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_dir = tmp_path / "session"
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


def test_resume_uses_original_session_store_and_reapplies_schema_and_workspace(tmp_path: Path) -> None:
    session_id = "22222222-2222-4222-8222-222222222222"
    query = FakeQuery([[_success(session_id)], [_success(session_id)]])
    runtime = _runtime(query)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_dir = tmp_path / "session"
    schema = {"type": "object", "required": ["answer"]}

    asyncio.run(
        runtime.run_agent(
            "First turn",
            AgentRole.COPYEDIT,
            workspace,
            schema,
            session_dir,
        )
    )
    result = asyncio.run(
        runtime.resume_agent(
            session_id,
            "Correct the response",
            AgentRole.COPYEDIT,
            workspace,
            schema,
            session_dir,
        )
    )

    assert result.status == "completed"
    _, options = query.calls[1]
    assert options.resume == session_id
    assert options.cwd == workspace.resolve()
    assert options.output_format == {"type": "json_schema", "schema": schema}
    assert "Corrector" in options.system_prompt
    assert query.loaded_sessions and query.loaded_sessions[0]


def test_resume_fails_without_persisted_session_instead_of_starting_fresh(tmp_path: Path) -> None:
    query = FakeQuery([[_success()]])
    runtime = _runtime(query)

    result = asyncio.run(
        runtime.resume_agent(
            "33333333-3333-4333-8333-333333333333",
            "Continue",
            AgentRole.CONSISTENCY,
            tmp_path,
            {"type": "object"},
            tmp_path / "missing-session",
        )
    )

    assert result.status == "failed"
    assert result.thread_id == "33333333-3333-4333-8333-333333333333"
    assert result.final_response is None
    assert result.error == "Claude session state is unavailable for 33333333-3333-4333-8333-333333333333."
    assert query.calls == []


def test_workspace_hook_allows_only_read_tools_inside_workspace(tmp_path: Path) -> None:
    query = FakeQuery([[_success()]])
    runtime = _runtime(query)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside)

    asyncio.run(
        runtime.run_agent(
            "Read files",
            AgentRole.FIGURE_REVIEW,
            workspace,
            {"type": "object"},
            tmp_path / "session",
        )
    )
    hook = query.calls[0][1].hooks["PreToolUse"][0].hooks[0]

    safe_read = asyncio.run(
        hook(
            {"tool_name": "Read", "tool_input": {"file_path": "inside.txt"}},
            None,
            {},
        )
    )
    safe_glob = asyncio.run(
        hook(
            {"tool_name": "Glob", "tool_input": {"pattern": "**/*.txt"}},
            None,
            {},
        )
    )
    safe_grep = asyncio.run(
        hook(
            {"tool_name": "Grep", "tool_input": {"pattern": "inside", "path": "."}},
            None,
            {},
        )
    )
    escaped_read = asyncio.run(
        hook(
            {"tool_name": "Read", "tool_input": {"file_path": "../outside.txt"}},
            None,
            {},
        )
    )
    symlink_read = asyncio.run(
        hook(
            {"tool_name": "Read", "tool_input": {"file_path": "outside-link"}},
            None,
            {},
        )
    )
    escaped_glob = asyncio.run(
        hook(
            {"tool_name": "Glob", "tool_input": {"pattern": "../*.txt"}},
            None,
            {},
        )
    )
    unknown_tool = asyncio.run(
        hook(
            {"tool_name": "Bash", "tool_input": {"command": "pwd"}},
            None,
            {},
        )
    )

    for allowed in (safe_read, safe_glob, safe_grep):
        assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
    for denied in (escaped_read, symlink_read, escaped_glob, unknown_tool):
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    ("message", "status", "error"),
    [
        (
            FakeResultMessage(
                subtype="error_during_execution",
                duration_ms=20,
                is_error=True,
                session_id="44444444-4444-4444-8444-444444444444",
                usage=None,
                errors=["provider failed", "retry exhausted"],
                terminal_reason="error",
            ),
            "failed",
            "provider failed; retry exhausted",
        ),
        (
            FakeResultMessage(
                subtype="error_during_execution",
                duration_ms=20,
                is_error=True,
                session_id="55555555-5555-4555-8555-555555555555",
                usage=None,
                result="cancelled",
                terminal_reason="aborted_tools",
            ),
            "interrupted",
            "cancelled",
        ),
        (
            FakeResultMessage(
                subtype="success",
                duration_ms=20,
                is_error=False,
                session_id="66666666-6666-4666-8666-666666666666",
                usage=None,
                structured_output=None,
            ),
            "failed",
            "Claude returned no structured output.",
        ),
    ],
)
def test_terminal_results_are_mapped_without_using_free_text(
    tmp_path: Path,
    message: FakeResultMessage,
    status: str,
    error: str,
) -> None:
    runtime = _runtime(FakeQuery([[message]]))

    result = asyncio.run(
        runtime.run_agent(
            "Review",
            AgentRole.SUBSTANTIVE_REVIEW,
            tmp_path,
            {"type": "object"},
            tmp_path / "session",
        )
    )

    assert result.status == status
    assert result.error == error
    assert result.final_response is None
    assert result.usage == AgentUsage()


class RaisingQuery:
    def __call__(self, *, prompt: str, options: FakeOptions):
        async def stream():
            yield FakeAssistantMessage("started", "77777777-7777-4777-8777-777777777777")
            raise RuntimeError("transport failed")

        return stream()


def test_query_exception_preserves_known_session_and_trace(tmp_path: Path) -> None:
    runtime = _runtime(RaisingQuery())

    result = asyncio.run(
        runtime.run_agent(
            "Review",
            AgentRole.SUBSTANTIVE_REVIEW,
            tmp_path,
            {"type": "object"},
            tmp_path / "session",
        )
    )

    assert result.status == "failed"
    assert result.thread_id == "77777777-7777-4777-8777-777777777777"
    assert result.error == "transport failed"
    trace = [json.loads(line) for line in result.trace_jsonl.splitlines()]
    assert trace[0]["message"]["text"] == "started"
    assert trace[-1]["status"] == "failed"


class CancellingStream:
    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self) -> "CancellingStream":
        return self

    async def __anext__(self) -> Any:
        raise asyncio.CancelledError

    async def aclose(self) -> None:
        self.closed = True


def test_cancellation_closes_native_query_and_propagates(tmp_path: Path) -> None:
    stream = CancellingStream()
    runtime = _runtime(lambda **_kwargs: stream)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runtime.run_agent(
                "Review",
                AgentRole.SUBSTANTIVE_REVIEW,
                tmp_path,
                {"type": "object"},
                tmp_path / "session",
            )
        )

    assert stream.closed is True


def test_runtime_requires_exact_sdk_version() -> None:
    with pytest.raises(RuntimeUnavailable, match="requires claude-agent-sdk==0.2.128"):
        ClaudeCodeAgentRuntime(
            route="claude_review",
            model="claude-test",
            provider="anthropic",
            reasoning="high",
            sdk_loader=lambda: _sdk(FakeQuery([]), version="0.2.127"),
        )
