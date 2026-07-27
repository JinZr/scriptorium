from __future__ import annotations

import asyncio
from pathlib import Path

from scriptorium.domain import AgentRole

from ._fake_sdk import FakeQuery, _runtime, _success


def test_workspace_hook_allows_only_read_tools_inside_workspace(tmp_path: Path) -> None:
    query = FakeQuery([[_success()]])
    runtime = _runtime(query)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside)

    asyncio.run(
        runtime.run_agent(
            "Read files",
            AgentRole.FIGURE_REVIEW,
            workspace,
            {"type": "object"},
            tmp_path / "session",
        )
    )
    hook = query.calls[0][1].hooks["PreToolUse"][0].hooks[0]

    safe_read = asyncio.run(
        hook(
            {"tool_name": "Read", "tool_input": {"file_path": "inside.txt"}},
            None,
            {},
        )
    )
    safe_glob = asyncio.run(
        hook(
            {"tool_name": "Glob", "tool_input": {"pattern": "**/*.txt"}},
            None,
            {},
        )
    )
    safe_grep = asyncio.run(
        hook(
            {"tool_name": "Grep", "tool_input": {"pattern": "inside", "path": "."}},
            None,
            {},
        )
    )
    escaped_read = asyncio.run(
        hook(
            {"tool_name": "Read", "tool_input": {"file_path": "../outside.txt"}},
            None,
            {},
        )
    )
    symlink_read = asyncio.run(
        hook(
            {"tool_name": "Read", "tool_input": {"file_path": "outside-link"}},
            None,
            {},
        )
    )
    escaped_glob = asyncio.run(
        hook(
            {"tool_name": "Glob", "tool_input": {"pattern": "../*.txt"}},
            None,
            {},
        )
    )
    unknown_tool = asyncio.run(
        hook(
            {"tool_name": "Bash", "tool_input": {"command": "pwd"}},
            None,
            {},
        )
    )

    for allowed in (safe_read, safe_glob, safe_grep):
        assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
    for denied in (escaped_read, symlink_read, escaped_glob, unknown_tool):
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
