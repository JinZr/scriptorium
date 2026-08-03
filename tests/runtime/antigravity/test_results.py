from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from scriptorium.domain import AgentRole
from scriptorium.runtime import AgentCancelled

from ._fake_sdk import (
    FakeAgent,
    FakeAntigravityCancelledError,
    FakeResponse,
    FakeStep,
    FakeUsage,
    make_runtime,
    make_sdk,
)


def test_missing_structured_output_and_sdk_errors_are_failed_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_output_sdk = make_sdk(
        response=FakeResponse(structured_output=None, usage=FakeUsage()),
        current_steps=[FakeStep("finish", "DONE", type="FINISH")],
    )
    missing_output = asyncio.run(
        make_runtime(monkeypatch, missing_output_sdk).run_agent(
            "Review.",
            AgentRole.CONSISTENCY,
            tmp_path,
            {"type": "object"},
            tmp_path / "state-a",
        )
    )
    assert missing_output.status == "failed"
    assert missing_output.final_response is None
    assert missing_output.error == "Antigravity turn did not return structured output."

    failed_sdk = make_sdk(
        response=FakeResponse(error=RuntimeError("resume state missing")),
        current_steps=[FakeStep("error", "ERROR", error="resume state missing")],
        conversation_id="abcdef12-1234-1234-1234-123456789012",
    )
    failed = asyncio.run(
        make_runtime(monkeypatch, failed_sdk).resume_agent(
            "abcdef12-1234-1234-1234-123456789012",
            "Resume.",
            AgentRole.REVISION,
            tmp_path,
            {"type": "object"},
            tmp_path / "state-b",
        )
    )
    assert failed.status == "failed"
    assert failed.error == "resume state missing"
    assert failed.thread_id == "abcdef12-1234-1234-1234-123456789012"
    assert json.loads(failed.trace_jsonl.splitlines()[0])["step"]["id"] == "error"


@pytest.mark.parametrize("recoverable_status", ["ERROR", "CANCELED"])
def test_recoverable_step_failure_before_successful_finish_is_completed(
    recoverable_status: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = make_sdk(
        response=FakeResponse(structured_output={"summary": "ok"}, usage=FakeUsage()),
        current_steps=[
            FakeStep("recoverable", recoverable_status, error="tool failed"),
            FakeStep("finish", "DONE", type="FINISH"),
        ],
    )

    result = asyncio.run(
        make_runtime(monkeypatch, sdk).run_agent(
            "Review.",
            AgentRole.CONSISTENCY,
            tmp_path,
            {"type": "object"},
            tmp_path / "state",
        )
    )

    assert result.status == "completed"
    assert result.final_response == '{"summary":"ok"}'
    assert result.error is None


@pytest.mark.parametrize(
    ("finish_status", "expected_status"),
    [("ERROR", "failed"), ("CANCELED", "interrupted")],
)
def test_terminal_finish_failure_controls_turn_status(
    finish_status: str,
    expected_status: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = make_sdk(
        response=FakeResponse(structured_output={"summary": "stale"}, usage=FakeUsage()),
        current_steps=[
            FakeStep("finish", finish_status, error="terminal failure", type="FINISH"),
        ],
    )

    result = asyncio.run(
        make_runtime(monkeypatch, sdk).run_agent(
            "Review.",
            AgentRole.CONSISTENCY,
            tmp_path,
            {"type": "object"},
            tmp_path / "state",
        )
    )

    assert result.status == expected_status
    assert result.final_response is None
    assert result.error == "terminal failure"


def test_structured_output_without_current_finish_is_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = make_sdk(
        response=FakeResponse(structured_output={"summary": "from prior turn"}, usage=FakeUsage()),
        current_steps=[FakeStep("tool", "DONE", type="TOOL")],
        prior_history=[FakeStep("old-finish", "DONE", type="FINISH")],
    )

    result = asyncio.run(
        make_runtime(monkeypatch, sdk).resume_agent(
            "abcdef12-1234-1234-1234-123456789012",
            "Resume.",
            AgentRole.REVISION,
            tmp_path,
            {"type": "object"},
            tmp_path / "state",
        )
    )

    assert result.status == "failed"
    assert result.final_response is None
    assert result.error == "Antigravity turn did not complete with a successful FINISH step."


def test_sdk_cancellation_is_interrupted_and_external_cancellation_is_reraised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk_cancelled_response = FakeResponse(error=FakeAntigravityCancelledError("backend cancelled"))
    sdk_cancelled_sdk = make_sdk(
        response=sdk_cancelled_response,
        current_steps=[FakeStep("cancelled", "CANCELED")],
    )
    interrupted = asyncio.run(
        make_runtime(monkeypatch, sdk_cancelled_sdk).run_agent(
            "Review.",
            AgentRole.COPYEDIT,
            tmp_path,
            {"type": "object"},
            tmp_path / "state-a",
        )
    )
    assert interrupted.status == "interrupted"
    assert interrupted.error == "backend cancelled"
    assert sdk_cancelled_response.cancelled is False
    assert FakeAgent.instances[0].conversation.cancelled is True

    external_cancelled_response = FakeResponse(error=asyncio.CancelledError())
    external_cancelled_sdk = make_sdk(
        response=external_cancelled_response,
        current_steps=[FakeStep("cancelled", "CANCELED")],
    )
    with pytest.raises(AgentCancelled) as caught:
        asyncio.run(
            make_runtime(monkeypatch, external_cancelled_sdk).run_agent(
                "Review.",
                AgentRole.COPYEDIT,
                tmp_path,
                {"type": "object"},
                tmp_path / "state-b",
            )
        )
    assert caught.value.result.status == "interrupted"
    assert external_cancelled_response.cancelled is False
    assert FakeAgent.instances[0].conversation.cancelled is True


def test_cancellation_reports_a_conversation_id_that_appears_during_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    late_thread_id = "87654321-4321-4321-4321-210987654321"
    sdk = make_sdk(
        response=FakeResponse(error=asyncio.CancelledError()),
        current_steps=[FakeStep("cancelled", "CANCELED")],
        conversation_id=None,
        cancelled_conversation_id=late_thread_id,
    )
    seen: list[str] = []

    with pytest.raises(AgentCancelled) as caught:
        asyncio.run(
            make_runtime(monkeypatch, sdk).run_agent(
                "Review.",
                AgentRole.COPYEDIT,
                tmp_path,
                {"type": "object"},
                tmp_path / "state-late-session",
                on_session_started=seen.append,
            )
        )

    assert seen == [late_thread_id]
    assert caught.value.result.thread_id == late_thread_id


@pytest.mark.parametrize("native_cancellation", [False, True])
def test_agent_cleanup_failure_is_diagnostic_without_reclassifying_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_cancellation: bool,
) -> None:
    cancellation = (
        FakeAntigravityCancelledError("backend cancelled") if native_cancellation else asyncio.CancelledError()
    )
    sdk = make_sdk(
        response=FakeResponse(error=cancellation),
        current_steps=[FakeStep("cancelled", "CANCELED")],
        exit_error=RuntimeError("close failed"),
    )

    invocation = make_runtime(monkeypatch, sdk).run_agent(
        "Review.",
        AgentRole.COPYEDIT,
        tmp_path,
        {"type": "object"},
        tmp_path / "state-cleanup-failure",
    )
    if native_cancellation:
        result = asyncio.run(invocation)
    else:
        with pytest.raises(AgentCancelled) as caught:
            asyncio.run(invocation)
        result = caught.value.result

    assert result.status == "interrupted"
    assert "close failed" in (result.error or "")


def test_cancellation_during_session_callback_still_uses_native_cancel_and_preserves_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = make_sdk(
        response=FakeResponse(structured_output={"summary": "unused"}),
        current_steps=[],
        exit_error=RuntimeError("close failed"),
    )

    async def scenario() -> tuple[AgentCancelled, int]:
        callback_started = asyncio.Event()
        callback_calls = 0

        async def session_started(_thread_id: str) -> None:
            nonlocal callback_calls
            callback_calls += 1
            if callback_calls == 1:
                callback_started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(
            make_runtime(monkeypatch, sdk).run_agent(
                "Review.",
                AgentRole.COPYEDIT,
                tmp_path,
                {"type": "object"},
                tmp_path / "state-callback-cancel",
                on_session_started=session_started,
            )
        )
        await callback_started.wait()
        task.cancel()
        with pytest.raises(AgentCancelled) as caught:
            await task
        return caught.value, callback_calls

    cancelled, callback_calls = asyncio.run(scenario())

    assert cancelled.result.status == "interrupted"
    assert "close failed" in (cancelled.result.error or "")
    assert callback_calls == 2
    assert FakeAgent.instances[0].conversation.cancelled is True


def test_cancellation_during_agent_exit_preserves_interrupted_result_and_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = make_sdk(
        response=FakeResponse(structured_output={"summary": "ok"}, usage=FakeUsage()),
        current_steps=[FakeStep("finish", "DONE", type="FINISH")],
        exit_error=RuntimeError("close failed"),
    )

    async def scenario() -> AgentCancelled:
        exit_started = asyncio.Event()
        FakeAgent.exit_started_event = exit_started
        FakeAgent.exit_release_event = asyncio.Event()
        task = asyncio.create_task(
            make_runtime(monkeypatch, sdk).run_agent(
                "Review.",
                AgentRole.COPYEDIT,
                tmp_path,
                {"type": "object"},
                tmp_path / "state-exit-cancel",
            )
        )
        await exit_started.wait()
        task.cancel()
        with pytest.raises(AgentCancelled) as caught:
            await task
        return caught.value

    cancelled = asyncio.run(scenario())

    assert cancelled.result.status == "interrupted"
    assert cancelled.result.usage.input_tokens == 37
    assert "close failed" in (cancelled.result.error or "")


def test_session_callback_error_is_not_mapped_to_provider_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = make_sdk(
        response=FakeResponse(structured_output={"summary": "ok"}, usage=FakeUsage()),
        current_steps=[FakeStep("finish", "DONE", type="FINISH")],
    )

    async def fail_callback(_thread_id: str) -> None:
        raise RuntimeError("session persistence failed")

    with pytest.raises(RuntimeError, match="session persistence failed"):
        asyncio.run(
            make_runtime(monkeypatch, sdk).run_agent(
                "Review.",
                AgentRole.COPYEDIT,
                tmp_path,
                {"type": "object"},
                tmp_path / "state-c",
                on_session_started=fail_callback,
            )
        )

    assert FakeAgent.instances[0].chat_calls == []
