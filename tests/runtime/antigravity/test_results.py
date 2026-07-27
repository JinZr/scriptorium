from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from scriptorium.domain import AgentRole

from ._fake_sdk import FakeAntigravityCancelledError, FakeResponse, FakeStep, FakeUsage, make_runtime, make_sdk


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
    assert sdk_cancelled_response.cancelled is True

    external_cancelled_response = FakeResponse(error=asyncio.CancelledError())
    external_cancelled_sdk = make_sdk(
        response=external_cancelled_response,
        current_steps=[FakeStep("cancelled", "CANCELED")],
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            make_runtime(monkeypatch, external_cancelled_sdk).run_agent(
                "Review.",
                AgentRole.COPYEDIT,
                tmp_path,
                {"type": "object"},
                tmp_path / "state-b",
            )
        )
    assert external_cancelled_response.cancelled is True
