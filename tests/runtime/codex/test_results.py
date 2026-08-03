from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from scriptorium.domain import AgentRole
from scriptorium.runtime import AgentCancelled, AgentUsage

from ._fake_sdk import (
    FakeCancellableHandle,
    FakeCancellableThread,
    FakeClient,
    FakeStatus,
    FakeStreamingThread,
    FakeThread,
    make_result,
    make_runtime,
)


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


@pytest.mark.parametrize("start_delay", [0.0, 0.01])
def test_external_cancellation_interrupts_known_or_late_turn_handle(
    start_delay: float,
    tmp_path: Path,
) -> None:
    handle = FakeCancellableHandle()
    thread = FakeCancellableThread(handle, start_delay=start_delay)
    client = FakeClient(thread)

    async def scenario() -> AgentCancelled:
        task = asyncio.create_task(
            make_runtime(client).run_agent(
                "Review.",
                AgentRole.FIGURE_REVIEW,
                tmp_path,
                {"type": "object"},
                tmp_path / "session",
            )
        )
        await thread.turn_started.wait()
        if not start_delay:
            await handle.streaming.wait()
        task.cancel()
        with pytest.raises(AgentCancelled) as caught:
            await task
        return caught.value

    cancelled = asyncio.run(scenario())

    assert handle.interrupted is True
    assert cancelled.result.status == "interrupted"
    assert cancelled.result.thread_id == "thread_cancel"
    assert cancelled.result.duration_ms == 17
    assert client.exited is True
    trace = [json.loads(line) for line in cancelled.result.trace_jsonl.splitlines()]
    assert trace[-1]["status"] == "interrupted"


def test_interrupt_failure_is_diagnostic_without_losing_cancellation(tmp_path: Path) -> None:
    handle = FakeCancellableHandle(
        interrupt_error=RuntimeError("interrupt rpc failed"),
        terminal_error=RuntimeError("terminal cancellation"),
    )
    thread = FakeCancellableThread(handle)

    async def scenario() -> AgentCancelled:
        task = asyncio.create_task(
            make_runtime(FakeClient(thread)).run_agent(
                "Review.",
                AgentRole.FIGURE_REVIEW,
                tmp_path,
                {"type": "object"},
                tmp_path / "session",
            )
        )
        await handle.streaming.wait()
        task.cancel()
        with pytest.raises(AgentCancelled) as caught:
            await task
        return caught.value

    cancelled = asyncio.run(scenario())

    assert cancelled.result.status == "interrupted"
    assert "terminal cancellation" in (cancelled.result.error or "")
    assert "interrupt rpc failed" in (cancelled.result.error or "")


def test_client_cleanup_failure_is_diagnostic_without_reclassifying_cancellation(tmp_path: Path) -> None:
    handle = FakeCancellableHandle()
    thread = FakeCancellableThread(handle)
    client = FakeClient(thread, exit_error=RuntimeError("close failed"))

    async def scenario() -> AgentCancelled:
        task = asyncio.create_task(
            make_runtime(client).run_agent(
                "Review.",
                AgentRole.FIGURE_REVIEW,
                tmp_path,
                {"type": "object"},
                tmp_path / "session",
            )
        )
        await handle.streaming.wait()
        task.cancel()
        with pytest.raises(AgentCancelled) as caught:
            await task
        return caught.value

    cancelled = asyncio.run(scenario())

    assert cancelled.result.status == "interrupted"
    assert "close failed" in (cancelled.result.error or "")


def test_known_turn_id_survives_a_nonterminal_cancel_drain(tmp_path: Path, monkeypatch) -> None:
    handle = FakeCancellableHandle(complete_on_interrupt=False)
    thread = FakeCancellableThread(handle)
    monkeypatch.setattr("scriptorium.runtime.codex._CANCEL_DRAIN_SECONDS", 0.01)

    async def scenario() -> AgentCancelled:
        task = asyncio.create_task(
            make_runtime(FakeClient(thread)).run_agent(
                "Review.",
                AgentRole.FIGURE_REVIEW,
                tmp_path,
                {"type": "object"},
                tmp_path / "session",
            )
        )
        await handle.streaming.wait()
        task.cancel()
        with pytest.raises(AgentCancelled) as caught:
            await task
        return caught.value

    cancelled = asyncio.run(scenario())
    trace = [json.loads(line) for line in cancelled.result.trace_jsonl.splitlines()]

    assert cancelled.result.status == "interrupted"
    assert any(record.get("turn_id") == handle.id for record in trace)
