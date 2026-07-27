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
        current_steps=[FakeStep("finish", "DONE")],
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
