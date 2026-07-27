from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from enum import Enum
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scriptorium.domain import AgentRole
from scriptorium.runtime import AgentResult, AgentRuntime, AgentUsage, CodexAgentRuntime, RuntimeUnavailable


class FakeStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass
class FakeItem:
    kind: str
    text: str


class FakeThread:
    def __init__(self, thread_id: str, result: Any = None, error: Exception | None = None) -> None:
        self.id = thread_id
        self.result = result
        self.error = error
        self.run_calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, task: str, **kwargs: Any) -> Any:
        self.run_calls.append((task, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


class FakeStreamingThread:
    def __init__(self) -> None:
        self.id = "thread_stream"
        self.turn_calls: list[tuple[str, dict[str, Any]]] = []

    async def turn(self, task: str, **kwargs: Any) -> Any:
        self.turn_calls.append((task, kwargs))
        usage = make_result().usage
        item = SimpleNamespace(type="agentMessage", phase=SimpleNamespace(value="final_answer"), text='{"ok":true}')
        completed = SimpleNamespace(
            id="turn_stream",
            status=FakeStatus.COMPLETED,
            error=None,
            duration_ms=9,
            items=[],
        )
        notifications = [
            SimpleNamespace(
                method="item/completed",
                payload=SimpleNamespace(turn_id="turn_stream", item=item),
            ),
            SimpleNamespace(
                method="thread/tokenUsage/updated",
                payload=SimpleNamespace(turn_id="turn_stream", token_usage=usage),
            ),
            SimpleNamespace(
                method="turn/completed",
                payload=SimpleNamespace(turn=completed),
            ),
        ]
        return SimpleNamespace(id="turn_stream", stream=lambda: _notification_stream(notifications))


class FakeClient:
    def __init__(self, start_thread: FakeThread, resume_thread: FakeThread | None = None) -> None:
        self.start_thread = start_thread
        self.resume_thread = resume_thread or start_thread
        self.start_calls: list[dict[str, Any]] = []
        self.resume_calls: list[tuple[str, dict[str, Any]]] = []
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> "FakeClient":
        self.entered = True
        return self

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.exited = True

    async def thread_start(self, **kwargs: Any) -> FakeThread:
        self.start_calls.append(kwargs)
        return self.start_thread

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> FakeThread:
        self.resume_calls.append((thread_id, kwargs))
        return self.resume_thread


def make_runtime(client: FakeClient) -> CodexAgentRuntime:
    return CodexAgentRuntime(
        route="primary",
        model="test-model",
        provider="test-provider",
        reasoning="high",
        client_factory=lambda: client,
    )


async def _notification_stream(notifications: list[Any]):
    for notification in notifications:
        yield notification


def make_result(status: FakeStatus = FakeStatus.COMPLETED) -> SimpleNamespace:
    usage = SimpleNamespace(
        last=SimpleNamespace(
            input_tokens=31,
            cached_input_tokens=7,
            output_tokens=11,
            reasoning_output_tokens=5,
            total_tokens=42,
        ),
        total=SimpleNamespace(
            input_tokens=99,
            cached_input_tokens=20,
            output_tokens=30,
            reasoning_output_tokens=12,
            total_tokens=129,
        ),
    )
    error = SimpleNamespace(message="model failed") if status is FakeStatus.FAILED else None
    return SimpleNamespace(
        id="turn_1",
        status=status,
        error=error,
        duration_ms=123,
        final_response='{"summary":"ok"}',
        items=[FakeItem(kind="agentMessage", text="done")],
        usage=usage,
    )


def test_run_agent_passes_codex_settings_and_normalizes_result(tmp_path: Path) -> None:
    thread = FakeThread("thread_1", result=make_result())
    client = FakeClient(thread)
    runtime = make_runtime(client)
    schema = {"type": "object", "required": ["summary"]}

    result = asyncio.run(
        runtime.run_agent(
            "Review the manuscript.",
            AgentRole.SUBSTANTIVE_REVIEW,
            tmp_path,
            schema,
            tmp_path / "session",
        )
    )

    assert isinstance(runtime, AgentRuntime)
    assert result == AgentResult(
        thread_id="thread_1",
        status="completed",
        final_response='{"summary":"ok"}',
        usage=AgentUsage(input_tokens=31, cached_input_tokens=7, output_tokens=11, reasoning_tokens=5),
        trace_jsonl=result.trace_jsonl,
        runtime_name="codex",
        runtime_version="injected",
        model="test-model",
        model_provider="test-provider",
        duration_ms=123,
        error=None,
    )
    assert client.entered and client.exited
    assert client.start_calls == [
        {
            "approval_mode": "deny_all",
            "config": {
                "model_reasoning_effort": "high",
                "project_root_markers": ["manifest.json"],
            },
            "cwd": str(tmp_path.resolve()),
            "developer_instructions": (
                "You are Scholiast, the Scriptorium substantive_review agent. "
                "Claims, evidence, methods, and scientific review. Work read-only and do not modify files. "
                "Return only output matching the supplied JSON schema."
            ),
            "model": "test-model",
            "model_provider": "test-provider",
            "sandbox": "read-only",
        }
    ]
    assert thread.run_calls == [
        (
            "Review the manuscript.",
            {
                "effort": "high",
                "model": "test-model",
                "output_schema": schema,
                "sandbox": "read-only",
            },
        )
    ]

    trace = [json.loads(line) for line in result.trace_jsonl.splitlines()]
    assert trace == [
        {"kind": "turn", "status": "completed", "turn_id": "turn_1"},
        {"item": {"kind": "agentMessage", "text": "done"}, "kind": "item"},
        {
            "kind": "usage",
            "usage": {
                "cached_input_tokens": 7,
                "input_tokens": 31,
                "output_tokens": 11,
                "reasoning_tokens": 5,
            },
        },
    ]
    assert json.loads(json.dumps(asdict(result)))["usage"]["reasoning_tokens"] == 5


def test_resume_agent_reapplies_workspace_role_and_schema(tmp_path: Path) -> None:
    resumed = FakeThread("thread_resumed", result=make_result(FakeStatus.INTERRUPTED))
    client = FakeClient(FakeThread("unused"), resume_thread=resumed)
    runtime = make_runtime(client)
    schema = {"type": "object"}

    result = asyncio.run(
        runtime.resume_agent(
            "thread_original",
            "Correct the invalid anchors.",
            AgentRole.SUBSTANTIVE_REVIEW,
            tmp_path,
            schema,
            tmp_path / "session",
        )
    )

    assert result.thread_id == "thread_resumed"
    assert result.status == "interrupted"
    assert client.resume_calls == [
        (
            "thread_original",
            {
                "approval_mode": "deny_all",
                "config": {
                    "model_reasoning_effort": "high",
                    "project_root_markers": ["manifest.json"],
                },
                "cwd": str(tmp_path.resolve()),
                "developer_instructions": (
                    "You are Scholiast, the Scriptorium substantive_review agent. "
                    "Claims, evidence, methods, and scientific review. Work read-only and do not modify files. "
                    "Return only output matching the supplied JSON schema."
                ),
                "model": "test-model",
                "model_provider": "test-provider",
                "sandbox": "read-only",
            },
        )
    ]
    assert resumed.run_calls == [
        (
            "Correct the invalid anchors.",
            {
                "effort": "high",
                "model": "test-model",
                "output_schema": schema,
                "sandbox": "read-only",
            },
        )
    ]


def test_streamed_turn_notifications_are_preserved_as_jsonl(tmp_path: Path) -> None:
    thread = FakeStreamingThread()
    client = FakeClient(thread)
    result = asyncio.run(
        make_runtime(client).run_agent(
            "Review.",
            AgentRole.SUBSTANTIVE_REVIEW,
            tmp_path,
            {"type": "object"},
            tmp_path / "session",
        )
    )

    assert result.status == "completed"
    assert result.final_response == '{"ok":true}'
    trace = [json.loads(line) for line in result.trace_jsonl.splitlines()]
    assert [record["kind"] for record in trace] == [
        "notification",
        "notification",
        "notification",
        "normalized_result",
    ]
    assert [record["method"] for record in trace[:-1]] == [
        "item/completed",
        "thread/tokenUsage/updated",
        "turn/completed",
    ]
    assert thread.turn_calls[0][1]["output_schema"] == {"type": "object"}


def test_failed_turn_is_returned_without_sdk_objects(tmp_path: Path) -> None:
    thread = FakeThread("thread_failed", result=make_result(FakeStatus.FAILED))
    result = asyncio.run(
        make_runtime(FakeClient(thread)).run_agent(
            "Review.",
            AgentRole.COPYEDIT,
            Path("."),
            {"type": "object"},
            tmp_path / "session",
        )
    )

    assert result.status == "failed"
    assert result.error == "model failed"
    assert result.thread_id == "thread_failed"
    json.dumps(asdict(result))


def test_sdk_exception_is_mapped_to_failed_result(tmp_path: Path) -> None:
    thread = FakeThread("thread_error", error=RuntimeError("transport closed"))
    result = asyncio.run(
        make_runtime(FakeClient(thread)).run_agent(
            "Review.",
            AgentRole.CONSISTENCY,
            Path("."),
            {"type": "object"},
            tmp_path / "session",
        )
    )

    assert result.status == "failed"
    assert result.thread_id == "thread_error"
    assert result.error == "transport closed"
    assert result.usage == AgentUsage()
    assert result.trace_jsonl == ""


def test_sdk_timeout_is_mapped_to_failed_result(tmp_path: Path) -> None:
    thread = FakeThread("thread_timeout", error=TimeoutError("turn timed out"))
    result = asyncio.run(
        make_runtime(FakeClient(thread)).run_agent(
            "Review.",
            AgentRole.FIGURE_REVIEW,
            Path("."),
            {"type": "object"},
            tmp_path / "session",
        )
    )

    assert result.status == "failed"
    assert result.thread_id == "thread_timeout"
    assert result.error == "turn timed out"


def test_missing_sdk_raises_clear_runtime_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_sdk(name: str) -> Any:
        assert name == "openai_codex"
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("scriptorium.runtime.import_module", missing_sdk)

    with pytest.raises(RuntimeUnavailable, match=r"openai-codex==0\.144\.4"):
        CodexAgentRuntime(route="primary", model="model", provider="openai", reasoning="high")


def test_wrong_sdk_version_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scriptorium.runtime.import_module",
        lambda name: SimpleNamespace(__version__="0.145.0"),
    )

    with pytest.raises(RuntimeUnavailable, match=r"found 0\.145\.0"):
        CodexAgentRuntime(route="primary", model="model", provider="openai", reasoning="high")
