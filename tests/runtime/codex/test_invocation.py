from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from scriptorium.domain import AgentRole
from scriptorium.runtime import AgentResult, AgentRuntime, AgentUsage

from ._fake_sdk import FakeClient, FakeStatus, FakeThread, make_result, make_runtime


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


def test_session_callback_runs_before_the_turn_and_errors_propagate(tmp_path: Path) -> None:
    thread = FakeThread("thread_1", result=make_result())
    runtime = make_runtime(FakeClient(thread))
    seen: list[str] = []

    result = asyncio.run(
        runtime.run_agent(
            "Review.",
            AgentRole.SUBSTANTIVE_REVIEW,
            tmp_path,
            {"type": "object"},
            tmp_path / "session",
            on_session_started=seen.append,
        )
    )

    assert result.status == "completed"
    assert seen == ["thread_1"]

    async def fail_callback(_thread_id: str) -> None:
        raise RuntimeError("session persistence failed")

    with pytest.raises(RuntimeError, match="session persistence failed"):
        asyncio.run(
            runtime.run_agent(
                "Review again.",
                AgentRole.SUBSTANTIVE_REVIEW,
                tmp_path,
                {"type": "object"},
                tmp_path / "session",
                on_session_started=fail_callback,
            )
        )
    assert thread.run_calls == [
        (
            "Review.",
            {
                "effort": "high",
                "model": "test-model",
                "output_schema": {"type": "object"},
                "sandbox": "read-only",
            },
        )
    ]
