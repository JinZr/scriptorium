import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import pytest

from scriptorium.domain import AgentRole
from scriptorium.runtime import RUNTIME_SDK_VERSIONS

from ._access import assert_retrieval
from ._retrieval import directory_digest, make_fixture


def _runtime(name: str, model: str, provider: str):
    if name == "codex":
        from scriptorium.runtime.codex import CodexAgentRuntime

        adapter = CodexAgentRuntime
    elif name == "claude_code":
        from scriptorium.runtime.claude_code import ClaudeCodeAgentRuntime

        adapter = ClaudeCodeAgentRuntime
    else:
        from scriptorium.runtime.antigravity import AntigravityAgentRuntime

        adapter = AntigravityAgentRuntime
    return adapter(route="live", model=model, provider=provider, reasoning="high")


async def _exercise_runtime(name: str, model: str, provider: str, tmp_path: Path) -> None:
    workspace = tmp_path / "bundle"
    cases = make_fixture(workspace)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    original_digest = directory_digest(workspace)
    thread_id = None
    for label, case in zip(("first", "resumed"), cases):
        started = time.monotonic()
        report = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "requested_runtime": name,
            "requested_sdk_version": RUNTIME_SDK_VERSIONS[name],
            "requested_model": model,
            "requested_provider": provider,
            "prompt": case.prompt,
            "schema": case.schema,
            "workspace_digest_before": original_digest,
        }
        try:
            runtime = _runtime(name, model, provider)
            arguments = (case.prompt, AgentRole.COPYEDIT, workspace, case.schema, session_dir)
            invocation = (
                runtime.run_agent(*arguments) if thread_id is None else runtime.resume_agent(thread_id, *arguments)
            )
            result = await asyncio.wait_for(invocation, timeout=300)
            (evidence / f"{label}.trace.jsonl").write_text(result.trace_jsonl, encoding="utf-8")
            normalized = asdict(result)
            normalized.pop("trace_jsonl")
            report.update(normalized)
        except Exception as exc:
            report.update(exception_type=type(exc).__name__, capability="unverified")
            raise
        finally:
            current_digest = directory_digest(workspace)
            report.update(
                elapsed_seconds=time.monotonic() - started,
                workspace_digest_after=current_digest,
            )
            (evidence / f"{label}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        assert current_digest == original_digest, "Runtime modified the frozen workspace"
        assert (result.runtime_name, result.runtime_version, result.model, result.model_provider) == (
            name,
            RUNTIME_SDK_VERSIONS[name],
            model,
            provider,
        )
        assert_retrieval(case, result, workspace)
        assert result.thread_id, "Missing native session identity"
        if thread_id is not None:
            assert result.thread_id == thread_id, "Resume created an unrelated session"
        thread_id = result.thread_id


@pytest.mark.live_harness
@pytest.mark.parametrize(
    "name,switch,provider",
    [("codex", "CODEX", "openai"), ("claude_code", "CLAUDE", "anthropic"), ("antigravity", "ANTIGRAVITY", "gemini")],
)
def test_live_retrieval_and_resume(tmp_path: Path, name: str, switch: str, provider: str) -> None:
    if os.environ.get(f"SCRIPTORIUM_LIVE_{switch}") != "1":
        pytest.skip(f"set SCRIPTORIUM_LIVE_{switch}=1 for the paid retrieval test; capability unverified")
    model = os.environ.get(f"SCRIPTORIUM_LIVE_{switch}_MODEL")
    assert model, f"SCRIPTORIUM_LIVE_{switch}_MODEL is required when live testing is enabled"
    if name == "codex":
        provider = os.environ.get("SCRIPTORIUM_LIVE_CODEX_PROVIDER", "openai")
        assert provider
    if name == "antigravity":
        assert os.environ.get("GEMINI_API_KEY"), "GEMINI_API_KEY is required when live testing is enabled"
    asyncio.run(_exercise_runtime(name, model, provider, tmp_path))
