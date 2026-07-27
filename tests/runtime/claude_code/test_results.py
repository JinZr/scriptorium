from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from scriptorium.domain import AgentRole
from scriptorium.runtime import AgentUsage

from ._fake_sdk import CancellingStream, FakeQuery, FakeResultMessage, RaisingQuery, _runtime


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
