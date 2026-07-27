from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from ..domain import ROLE_CATALOG, AgentRole

AgentStatus = Literal["completed", "failed", "interrupted"]
CODEX_SDK_VERSION = "0.144.4"
CLAUDE_SDK_VERSION = "0.2.128"
ANTIGRAVITY_SDK_VERSION = "0.1.8"
RUNTIME_SDK_VERSIONS = {
    "codex": CODEX_SDK_VERSION,
    "claude_code": CLAUDE_SDK_VERSION,
    "antigravity": ANTIGRAVITY_SDK_VERSION,
}


class RuntimeUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AgentUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AgentResult:
    thread_id: str | None
    status: AgentStatus
    final_response: str | None
    usage: AgentUsage
    trace_jsonl: str
    runtime_name: str
    runtime_version: str
    model: str
    model_provider: str
    duration_ms: int | None
    error: str | None


@runtime_checkable
class AgentRuntime(Protocol):
    async def run_agent(
        self,
        task: str,
        role: AgentRole,
        workspace: Path,
        schema: Mapping[str, object],
        session_dir: Path,
    ) -> AgentResult: ...

    async def resume_agent(
        self,
        thread_id: str,
        task: str,
        role: AgentRole,
        workspace: Path,
        schema: Mapping[str, object],
        session_dir: Path,
    ) -> AgentResult: ...


def _role_instructions(role: AgentRole) -> str:
    spec = ROLE_CATALOG[role]
    return (
        f"You are {spec.display_name}, the Scriptorium {role.value} agent. {spec.description}. "
        "Work read-only and do not modify files. Return only output matching the supplied JSON schema."
    )
