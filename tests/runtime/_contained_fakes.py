from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from scriptorium.config import RouteConfig
from scriptorium.domain import AgentRole
from scriptorium.runtime import AgentCancelled, AgentResult, AgentUsage
from scriptorium.runtime.contained import ContainedAgentRuntime


class ContainmentFakeRuntime:
    def __init__(self, route: RouteConfig, mode: str | None = None) -> None:
        self.route = route
        self.mode = mode or route.model

    async def run_agent(
        self,
        task,
        role,
        workspace,
        schema,
        session_dir,
        on_session_started=None,
    ):
        del task, workspace, schema
        if on_session_started is not None:
            pending = on_session_started("thread-contained")
            if asyncio.iscoroutine(pending):
                await pending
        state = {
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "role": role.value,
        }
        _write_json(session_dir / "worker-state.json", state)
        if self.mode == "complete":
            return _result(self.route, role, "completed", json.dumps(state))
        if self.mode == "exit":
            os._exit(17)
        if self.mode in {"hang", "hang_auth", "leak"}:
            if self.mode == "hang_auth":
                native_config = session_dir / "scriptorium-claude-forced-cleanup"
                native_config.mkdir()
                (native_config / ".credentials.json").write_text("secret", encoding="utf-8")
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            _write_json(session_dir / "provider-state.json", {"pid": child.pid, **state})
        if self.mode == "thread":
            try:
                await asyncio.to_thread(_block_native_thread, session_dir / "native-thread.json", state)
            except asyncio.CancelledError:
                _write_json(session_dir / "cancelled.json", state)
                raise AgentCancelled(_result(self.route, role, "interrupted", None))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            _write_json(session_dir / "cancelled.json", state)
            if self.mode in {"hang", "hang_auth"}:
                # Deliberately ignore native cancellation so the worker watchdog must reap the process group.
                await asyncio.Event().wait()
            raise AgentCancelled(_result(self.route, role, "interrupted", None))

    async def resume_agent(
        self,
        thread_id,
        task,
        role,
        workspace,
        schema,
        session_dir,
        on_session_started=None,
    ):
        del thread_id
        return await self.run_agent(
            task,
            role,
            workspace,
            schema,
            session_dir,
            on_session_started=on_session_started,
        )


def make_runtime(route: RouteConfig) -> ContainmentFakeRuntime:
    return ContainmentFakeRuntime(route)


def make_blocking_runtime(route: RouteConfig) -> ContainmentFakeRuntime:
    return ContainmentFakeRuntime(route, "block")


def _result(route: RouteConfig, role: AgentRole, status: str, response: str | None) -> AgentResult:
    return AgentResult(
        thread_id="thread-contained",
        status=status,
        final_response=response,
        usage=AgentUsage(input_tokens=3, output_tokens=2),
        trace_jsonl=json.dumps({"status": status, "role": role.value}) + "\n",
        runtime_name=route.runtime,
        runtime_version=route.runtime_version or "",
        model=route.model,
        model_provider=route.model_provider,
        duration_ms=1,
        error="cancelled" if status == "interrupted" else None,
    )


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _block_native_thread(path: Path, state: dict[str, object]) -> None:
    _write_json(path, state)
    time.sleep(120)


async def _run_owner(repo: Path) -> None:
    route = RouteConfig(
        name="contained",
        model_provider="openai",
        model=sys.argv[2] if len(sys.argv) > 2 else "block",
        input_usd_per_million=0,
        output_usd_per_million=0,
        runtime="codex",
        runtime_version="0.144.4",
    )
    runtime = ContainedAgentRuntime(
        route,
        repo,
        _worker_runtime_factory=f"{Path(__file__).resolve()}:make_runtime",
    )
    await runtime.run_agent(
        "task",
        AgentRole.COPYEDIT,
        repo / "workspace",
        {},
        repo / ".scriptorium" / "runs" / "run_owner" / "sessions" / "fake",
    )


if __name__ == "__main__":
    asyncio.run(_run_owner(Path(sys.argv[1]).resolve()))
