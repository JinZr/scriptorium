from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scriptorium.domain import AgentRole

from ._fake_sdk import FakeAgent, FakeResponse, FakeSessionContinuationMode, FakeStep, FakeUsage, make_runtime, make_sdk


def test_resume_is_strict_and_reuses_session_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = make_sdk(
        response=FakeResponse(structured_output={"corrected": True}, usage=FakeUsage()),
        current_steps=[FakeStep("resume-step", "DONE")],
    )
    runtime = make_runtime(monkeypatch, sdk)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_dir = tmp_path / "session"
    thread_id = "abcdef12-1234-1234-1234-123456789012"

    result = asyncio.run(
        runtime.resume_agent(
            thread_id,
            "Correct the anchors.",
            AgentRole.REVISION,
            workspace,
            {"type": "object"},
            session_dir,
        )
    )

    assert result.status == "completed"
    config = FakeAgent.instances[0].config.kwargs
    assert config["conversation_id"] == thread_id
    assert config["session_continuation_mode"] is FakeSessionContinuationMode.RESUME
    assert "CREATE_OR_RESUME" not in str(config["session_continuation_mode"])
    assert config["save_dir"] == str((session_dir / "save").resolve())
    assert config["app_data_dir"] == str((session_dir / "app").resolve())
