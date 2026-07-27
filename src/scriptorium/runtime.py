from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from importlib import import_module
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Protocol, runtime_checkable

from .domain import ROLE_CATALOG, AgentRole

AgentStatus = Literal["completed", "failed", "interrupted"]
CODEX_SDK_VERSION = "0.144.4"


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
    ) -> AgentResult: ...

    async def resume_agent(self, thread_id: str, task: str) -> AgentResult: ...


class CodexAgentRuntime:
    def __init__(
        self,
        *,
        route: str,
        model: str,
        provider: str,
        reasoning: str,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.route = route
        self.model = model
        self.provider = provider
        self.reasoning = reasoning

        if client_factory is None:
            try:
                sdk = import_module("openai_codex")
            except ImportError as exc:
                raise RuntimeUnavailable(
                    f"The Codex runtime requires openai-codex=={CODEX_SDK_VERSION}. "
                    "Install the Scriptorium project dependencies before running an agent."
                ) from exc
            if str(sdk.__version__) != CODEX_SDK_VERSION:
                raise RuntimeUnavailable(
                    f"The Codex runtime requires openai-codex=={CODEX_SDK_VERSION}, " f"but found {sdk.__version__}."
                )
            self._client_factory = sdk.AsyncCodex
            self._sandbox = sdk.Sandbox.read_only
            self._approval_mode = sdk.ApprovalMode.deny_all
            self._runtime_version = str(sdk.__version__)
        else:
            self._client_factory = client_factory
            self._sandbox = "read-only"
            self._approval_mode = "deny_all"
            self._runtime_version = "injected"

    async def run_agent(
        self,
        task: str,
        role: AgentRole,
        workspace: Path,
        schema: Mapping[str, object],
    ) -> AgentResult:
        thread_id: str | None = None
        notifications: list[dict[str, Any]] = []
        try:
            async with self._client_factory() as client:
                thread = await client.thread_start(
                    approval_mode=self._approval_mode,
                    config={
                        "model_reasoning_effort": self.reasoning,
                        "project_root_markers": ["manifest.json"],
                    },
                    cwd=str(workspace.resolve()),
                    developer_instructions=_role_instructions(role),
                    model=self.model,
                    model_provider=self.provider,
                    sandbox=self._sandbox,
                )
                thread_id = str(thread.id)
                turn = await _run_thread_turn(
                    thread,
                    task,
                    {
                        "effort": self.reasoning,
                        "model": self.model,
                        "output_schema": dict(schema),
                        "sandbox": self._sandbox,
                    },
                    notifications,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._failed_result(thread_id, exc, notifications)
        return self._normalize_result(thread_id, turn, notifications)

    async def resume_agent(self, thread_id: str, task: str) -> AgentResult:
        notifications: list[dict[str, Any]] = []
        try:
            async with self._client_factory() as client:
                thread = await client.thread_resume(
                    thread_id,
                    approval_mode=self._approval_mode,
                    config={
                        "model_reasoning_effort": self.reasoning,
                        "project_root_markers": ["manifest.json"],
                    },
                    model=self.model,
                    model_provider=self.provider,
                    sandbox=self._sandbox,
                )
                resumed_thread_id = str(thread.id)
                turn = await _run_thread_turn(
                    thread,
                    task,
                    {
                        "effort": self.reasoning,
                        "model": self.model,
                        "sandbox": self._sandbox,
                    },
                    notifications,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._failed_result(thread_id, exc, notifications)
        return self._normalize_result(resumed_thread_id, turn, notifications)

    def _normalize_result(
        self,
        thread_id: str,
        turn: Any,
        notifications: list[dict[str, Any]],
    ) -> AgentResult:
        status = _normalize_status(getattr(turn, "status", None))
        usage = _normalize_usage(getattr(turn, "usage", None))
        error = _error_message(getattr(turn, "error", None))
        if status == "failed" and error is None:
            error = "Codex turn failed"

        return AgentResult(
            thread_id=thread_id,
            status=status,
            final_response=_optional_string(getattr(turn, "final_response", None)),
            usage=usage,
            trace_jsonl=_trace_jsonl(
                getattr(turn, "id", None),
                status,
                getattr(turn, "items", ()),
                usage,
                notifications,
            ),
            runtime_name="codex",
            runtime_version=self._runtime_version,
            model=self.model,
            model_provider=self.provider,
            duration_ms=_optional_int(getattr(turn, "duration_ms", None)),
            error=error,
        )

    def _failed_result(
        self,
        thread_id: str | None,
        exc: Exception,
        notifications: list[dict[str, Any]] | None = None,
    ) -> AgentResult:
        return AgentResult(
            thread_id=thread_id,
            status="failed",
            final_response=None,
            usage=AgentUsage(),
            trace_jsonl=_notifications_jsonl(notifications or []),
            runtime_name="codex",
            runtime_version=self._runtime_version,
            model=self.model,
            model_provider=self.provider,
            duration_ms=None,
            error=str(exc) or "Codex runtime failed",
        )


def _role_instructions(role: AgentRole) -> str:
    spec = ROLE_CATALOG[role]
    return (
        f"You are {spec.display_name}, the Scriptorium {role.value} agent. {spec.description}. "
        "Work read-only and do not modify files. Return only output matching the supplied JSON schema."
    )


def _normalize_status(value: Any) -> AgentStatus:
    raw = getattr(value, "value", value)
    if raw == "completed":
        return "completed"
    if raw == "interrupted":
        return "interrupted"
    return "failed"


async def _run_thread_turn(
    thread: Any,
    task: str,
    kwargs: dict[str, Any],
    notifications: list[dict[str, Any]],
) -> Any:
    if not hasattr(thread, "turn"):
        return await thread.run(task, **kwargs)

    handle = await thread.turn(task, **kwargs)
    stream = handle.stream()
    items: list[Any] = []
    usage: Any = None
    completed_turn: Any = None
    try:
        async for notification in stream:
            method = str(getattr(notification, "method", "unknown"))
            payload = getattr(notification, "payload", None)
            notifications.append(
                {
                    "method": method,
                    "payload": _to_plain(payload),
                }
            )
            if method == "item/completed" and getattr(payload, "turn_id", None) == handle.id:
                items.append(getattr(payload, "item", None))
            elif method == "thread/tokenUsage/updated" and getattr(payload, "turn_id", None) == handle.id:
                usage = getattr(payload, "token_usage", None)
            elif method == "turn/completed":
                turn = getattr(payload, "turn", None)
                if turn is not None and str(getattr(turn, "id", "")) == str(handle.id):
                    completed_turn = turn
    finally:
        if hasattr(stream, "aclose"):
            await stream.aclose()
    if completed_turn is None:
        raise RuntimeError("turn completed event not received")
    if not items:
        items = list(getattr(completed_turn, "items", ()) or ())
    return SimpleNamespace(
        id=completed_turn.id,
        status=completed_turn.status,
        error=getattr(completed_turn, "error", None),
        duration_ms=getattr(completed_turn, "duration_ms", None),
        final_response=_final_response(items),
        items=items,
        usage=usage,
    )


def _normalize_usage(value: Any) -> AgentUsage:
    current = getattr(value, "last", value)
    if current is None:
        return AgentUsage()
    return AgentUsage(
        input_tokens=_token_count(current, "input_tokens", "inputTokens"),
        cached_input_tokens=_token_count(current, "cached_input_tokens", "cachedInputTokens"),
        output_tokens=_token_count(current, "output_tokens", "outputTokens"),
        reasoning_tokens=_token_count(
            current,
            "reasoning_output_tokens",
            "reasoningOutputTokens",
            "reasoning_tokens",
            "reasoningTokens",
        ),
    )


def _token_count(value: Any, *names: str) -> int:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return int(value[name])
        if hasattr(value, name):
            return int(getattr(value, name))
    return 0


def _trace_jsonl(
    turn_id: Any,
    status: AgentStatus,
    items: Any,
    usage: AgentUsage,
    notifications: list[dict[str, Any]],
) -> str:
    if notifications:
        records = [
            {
                "kind": "notification",
                "method": notification["method"],
                "payload": notification["payload"],
            }
            for notification in notifications
        ]
        records.append(
            {
                "kind": "normalized_result",
                "turn_id": _optional_string(turn_id),
                "status": status,
                "usage": asdict(usage),
            }
        )
        return "\n".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) for record in records
        )
    records = [{"kind": "turn", "turn_id": _optional_string(turn_id), "status": status}]
    records.extend({"kind": "item", "item": _to_plain(item)} for item in items or ())
    records.append({"kind": "usage", "usage": asdict(usage)})
    return "\n".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) for record in records
    )


def _notifications_jsonl(notifications: list[dict[str, Any]]) -> str:
    return "\n".join(
        json.dumps(
            {
                "kind": "notification",
                "method": notification["method"],
                "payload": notification["payload"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for notification in notifications
    )


def _final_response(items: list[Any]) -> str | None:
    unknown_phase: str | None = None
    for item in reversed(items):
        candidate = getattr(item, "root", item)
        if getattr(candidate, "type", None) != "agentMessage":
            continue
        text = _optional_string(getattr(candidate, "text", None))
        phase = getattr(getattr(candidate, "phase", None), "value", getattr(candidate, "phase", None))
        if phase == "final_answer":
            return text
        if phase is None and unknown_phase is None:
            unknown_phase = text
    return unknown_phase


def _to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return _to_plain(value.value)
    if isinstance(value, Mapping):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return _to_plain(value.model_dump(mode="json", by_alias=True))
    if is_dataclass(value):
        return _to_plain(asdict(value))
    if hasattr(value, "__dict__"):
        return {key: _to_plain(item) for key, item in vars(value).items() if not key.startswith("_")}
    return str(value)


def _error_message(value: Any) -> str | None:
    if value is None:
        return None
    message = value.get("message") if isinstance(value, Mapping) else getattr(value, "message", None)
    return _optional_string(message if message is not None else value)


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
