import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from scriptorium.domain import AgentRole
from scriptorium.runtime import AgentResult, AgentRuntime

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _directory_digest(path: Path) -> str:
    digest = sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        relative = item.relative_to(path).as_posix().encode()
        content = item.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


async def _exercise_runtime(runtime: AgentRuntime, tmp_path: Path) -> None:
    workspace = tmp_path / "bundle"
    workspace.mkdir()
    (workspace / "manifest.json").write_text('{"sources":["paper.txt"]}\n', encoding="utf-8")
    (workspace / "paper.txt").write_text("A short frozen manuscript.\n", encoding="utf-8")
    session_dir = tmp_path / "session"
    original_digest = _directory_digest(workspace)

    first = await runtime.run_agent(
        'Return exactly {"answer":"first"} as structured output.',
        AgentRole.COPYEDIT,
        workspace,
        SCHEMA,
        session_dir,
    )
    _assert_live_result(first)
    assert first.thread_id

    resumed = await runtime.resume_agent(
        first.thread_id,
        'Return exactly {"answer":"resumed"} as structured output.',
        AgentRole.COPYEDIT,
        workspace,
        SCHEMA,
        session_dir,
    )
    _assert_live_result(resumed)
    assert resumed.thread_id == first.thread_id
    assert _directory_digest(workspace) == original_digest


def _assert_live_result(result: AgentResult) -> None:
    assert result.status == "completed", result.error
    assert result.final_response is not None
    structured = json.loads(result.final_response)
    assert set(structured) == {"answer"}
    assert isinstance(structured["answer"], str) and structured["answer"]
    assert result.usage.input_tokens + result.usage.output_tokens > 0
    trace = [json.loads(line) for line in result.trace_jsonl.splitlines() if line]
    assert trace


@pytest.mark.live_harness
@pytest.mark.skipif(
    os.environ.get("SCRIPTORIUM_LIVE_CLAUDE") != "1",
    reason="set SCRIPTORIUM_LIVE_CLAUDE=1 to run the paid Claude Code smoke",
)
def test_live_claude_first_turn_and_resume(tmp_path: Path) -> None:
    from scriptorium.claude_runtime import ClaudeCodeAgentRuntime

    model = os.environ.get("SCRIPTORIUM_LIVE_CLAUDE_MODEL")
    assert model, "SCRIPTORIUM_LIVE_CLAUDE_MODEL is required when live Claude testing is enabled"
    runtime = ClaudeCodeAgentRuntime(route="live", model=model, provider="anthropic", reasoning="high")

    asyncio.run(_exercise_runtime(runtime, tmp_path))


@pytest.mark.live_harness
@pytest.mark.skipif(
    os.environ.get("SCRIPTORIUM_LIVE_ANTIGRAVITY") != "1",
    reason="set SCRIPTORIUM_LIVE_ANTIGRAVITY=1 to run the paid Antigravity smoke",
)
def test_live_antigravity_first_turn_and_resume(tmp_path: Path) -> None:
    from scriptorium.antigravity_runtime import AntigravityAgentRuntime

    model = os.environ.get("SCRIPTORIUM_LIVE_ANTIGRAVITY_MODEL")
    assert model, "SCRIPTORIUM_LIVE_ANTIGRAVITY_MODEL is required when live Antigravity testing is enabled"
    assert os.environ.get("GEMINI_API_KEY"), "GEMINI_API_KEY is required when live Antigravity testing is enabled"
    runtime = AntigravityAgentRuntime(route="live", model=model, provider="gemini", reasoning="high")

    asyncio.run(_exercise_runtime(runtime, tmp_path))
