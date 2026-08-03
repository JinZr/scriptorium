from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from scriptorium.domain import AgentRole
from scriptorium.runtime import AgentCancelled, AgentUsage

from ._fake_sdk import (
    CancellingStream,
    FakeMirrorErrorMessage,
    FakeQuery,
    FakeResultMessage,
    FixedStreamQuery,
    InterruptibleStream,
    RaisingQuery,
    _runtime,
    _success,
)


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


def test_completed_result_fails_when_session_mirror_is_missing(tmp_path: Path) -> None:
    async def stream():
        yield _success()

    runtime = _runtime(lambda **_kwargs: stream())

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
    assert result.final_response is None
    assert result.error == "Claude session state could not be persisted."
    assert result.usage.input_tokens == 18


def test_completed_result_fails_after_session_mirror_error(tmp_path: Path) -> None:
    runtime = _runtime(FakeQuery([[FakeMirrorErrorMessage(), _success()]]))

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
    assert result.final_response is None
    assert result.error == "Claude session state could not be persisted."
    assert result.usage.input_tokens == 18


def test_cancellation_closes_native_query_and_propagates(tmp_path: Path) -> None:
    stream = CancellingStream()
    native_config_dirs: list[Path] = []

    def query(**kwargs):
        native_config_dirs.append(Path(kwargs["options"].env["CLAUDE_CONFIG_DIR"]))
        return stream

    runtime = _runtime(query)

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
    assert len(native_config_dirs) == 1
    assert not native_config_dirs[0].exists()


def test_external_cancellation_interrupts_drains_and_disconnects(tmp_path: Path) -> None:
    terminal = FakeResultMessage(
        subtype="error_during_execution",
        duration_ms=44,
        is_error=True,
        session_id="99999999-9999-4999-8999-999999999999",
        usage={"input_tokens": 4, "output_tokens": 2},
        result="cancelled",
        terminal_reason="aborted_streaming",
    )
    stream = InterruptibleStream(terminal)
    query = FixedStreamQuery(stream)

    async def scenario() -> AgentCancelled:
        task = asyncio.create_task(
            _runtime(query).run_agent(
                "Review",
                AgentRole.SUBSTANTIVE_REVIEW,
                tmp_path,
                {"type": "object"},
                tmp_path / "session",
            )
        )
        while "receive_response" not in query.client_events:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(AgentCancelled) as caught:
            await task
        return caught.value

    cancelled = asyncio.run(scenario())

    assert cancelled.result.status == "interrupted"
    assert cancelled.result.thread_id == terminal.session_id
    assert cancelled.result.usage.input_tokens == 4
    assert cancelled.result.duration_ms == 44
    assert query.client_events == ["connect", "query", "receive_response", "interrupt", "disconnect"]
    assert stream.closed is True


def test_session_callback_error_is_not_mapped_to_provider_failure(tmp_path: Path) -> None:
    query = FakeQuery([[_success()]])

    async def fail_callback(_thread_id: str) -> None:
        raise RuntimeError("session persistence failed")

    with pytest.raises(RuntimeError, match="session persistence failed"):
        asyncio.run(
            _runtime(query).run_agent(
                "Review",
                AgentRole.SUBSTANTIVE_REVIEW,
                tmp_path,
                {"type": "object"},
                tmp_path / "session",
                on_session_started=fail_callback,
            )
        )

    assert query.interrupt_calls == 1
    assert query.disconnect_calls == 1
