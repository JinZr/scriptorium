from __future__ import annotations

import asyncio
from pathlib import Path

from scriptorium.domain import AgentRole

from ._fake_sdk import FakeQuery, _runtime, _success


def test_resume_uses_original_session_store_and_reapplies_schema_and_workspace(tmp_path: Path) -> None:
    session_id = "22222222-2222-4222-8222-222222222222"
    query = FakeQuery([[_success(session_id)], [_success(session_id)]])
    runtime = _runtime(query)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_dir = tmp_path / "session"
    schema = {"type": "object", "required": ["answer"]}

    asyncio.run(
        runtime.run_agent(
            "First turn",
            AgentRole.COPYEDIT,
            workspace,
            schema,
            session_dir,
        )
    )
    result = asyncio.run(
        runtime.resume_agent(
            session_id,
            "Correct the response",
            AgentRole.COPYEDIT,
            workspace,
            schema,
            session_dir,
        )
    )

    assert result.status == "completed"
    _, options = query.calls[1]
    assert options.resume == session_id
    assert options.cwd == workspace.resolve()
    assert options.output_format == {"type": "json_schema", "schema": schema}
    assert "Corrector" in options.system_prompt
    assert query.loaded_sessions and query.loaded_sessions[0]
    assert len(query.native_config_dirs) == 2
    assert query.native_config_dirs[0] != query.native_config_dirs[1]
    assert all(not path.exists() for path in query.native_config_dirs)


def test_resume_fails_without_persisted_session_instead_of_starting_fresh(tmp_path: Path) -> None:
    query = FakeQuery([[_success()]])
    runtime = _runtime(query)

    result = asyncio.run(
        runtime.resume_agent(
            "33333333-3333-4333-8333-333333333333",
            "Continue",
            AgentRole.CONSISTENCY,
            tmp_path,
            {"type": "object"},
            tmp_path / "missing-session",
        )
    )

    assert result.status == "failed"
    assert result.thread_id == "33333333-3333-4333-8333-333333333333"
    assert result.final_response is None
    assert result.error == "Claude session state is unavailable for 33333333-3333-4333-8333-333333333333."
    assert query.calls == []
