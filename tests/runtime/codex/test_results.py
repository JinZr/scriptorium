from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path

from scriptorium.domain import AgentRole
from scriptorium.runtime import AgentUsage

from ._fake_sdk import FakeClient, FakeStatus, FakeStreamingThread, FakeThread, make_result, make_runtime


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
