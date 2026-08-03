from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from importlib import import_module
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..domain import AgentRole
from .base import (
    CODEX_SDK_VERSION,
    AgentCancelled,
    AgentResult,
    AgentStatus,
    AgentUsage,
    RuntimeUnavailable,
    SessionStartedCallback,
    _notify_session_started,
    _role_instructions,
    _SessionStartedCallbackError,
)

_LATE_TURN_START_SECONDS = 1.0
_CANCEL_RPC_SECONDS = 2.0
_CANCEL_DRAIN_SECONDS = 2.0


class _CodexTurnCancelled(Exception):
    def __init__(self, turn: Any | None, cleanup_error: str | None, turn_id: str | None = None) -> None:
        self.turn = turn
        self.cleanup_error = cleanup_error
        self.turn_id = turn_id


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
        session_dir: Path,
        on_session_started: SessionStartedCallback | None = None,
    ) -> AgentResult:
        thread_id: str | None = None
        notifications: list[dict[str, Any]] = []
        turn: Any = None
        cancellation: _CodexTurnCancelled | asyncio.CancelledError | None = None
        callback_error: Exception | None = None
        runtime_error: Exception | None = None
        context_cleanup_error: str | None = None
        try:
            async with self._client_factory() as client:
                try:
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
                    await _notify_session_started(on_session_started, thread_id)
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
                except _SessionStartedCallbackError as exc:
                    callback_error = exc.error
                except _CodexTurnCancelled as exc:
                    cancellation = exc
                except asyncio.CancelledError as exc:
                    cancellation = exc
                except Exception as exc:
                    runtime_error = exc
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            else:
                context_cleanup_error = "Codex client cleanup was cancelled"
        except Exception as exc:
            if cancellation is not None or callback_error is not None or runtime_error is not None:
                context_cleanup_error = str(exc) or type(exc).__name__
            else:
                runtime_error = exc
        if callback_error is not None:
            if context_cleanup_error:
                raise callback_error from RuntimeError(context_cleanup_error)
            raise callback_error
        if cancellation is not None:
            cancelled_turn = cancellation.turn if isinstance(cancellation, _CodexTurnCancelled) else turn
            turn_id = cancellation.turn_id if isinstance(cancellation, _CodexTurnCancelled) else None
            cleanup_error = cancellation.cleanup_error if isinstance(cancellation, _CodexTurnCancelled) else None
            raise AgentCancelled(
                self._interrupted_result(
                    thread_id,
                    cancelled_turn,
                    notifications,
                    _join_errors(cleanup_error, context_cleanup_error),
                    turn_id,
                )
            )
        if runtime_error is not None:
            if context_cleanup_error:
                runtime_error = RuntimeError(
                    f"{str(runtime_error) or type(runtime_error).__name__}; " f"native cleanup: {context_cleanup_error}"
                )
            return self._failed_result(thread_id, runtime_error, notifications)
        return self._normalize_result(thread_id, turn, notifications)

    async def resume_agent(
        self,
        thread_id: str,
        task: str,
        role: AgentRole,
        workspace: Path,
        schema: Mapping[str, object],
        session_dir: Path,
        on_session_started: SessionStartedCallback | None = None,
    ) -> AgentResult:
        notifications: list[dict[str, Any]] = []
        resumed_thread_id = thread_id
        turn: Any = None
        cancellation: _CodexTurnCancelled | asyncio.CancelledError | None = None
        callback_error: Exception | None = None
        runtime_error: Exception | None = None
        context_cleanup_error: str | None = None
        try:
            async with self._client_factory() as client:
                try:
                    thread = await client.thread_resume(
                        thread_id,
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
                    resumed_thread_id = str(thread.id)
                    await _notify_session_started(on_session_started, resumed_thread_id)
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
                except _SessionStartedCallbackError as exc:
                    callback_error = exc.error
                except _CodexTurnCancelled as exc:
                    cancellation = exc
                except asyncio.CancelledError as exc:
                    cancellation = exc
                except Exception as exc:
                    runtime_error = exc
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            else:
                context_cleanup_error = "Codex client cleanup was cancelled"
        except Exception as exc:
            if cancellation is not None or callback_error is not None or runtime_error is not None:
                context_cleanup_error = str(exc) or type(exc).__name__
            else:
                runtime_error = exc
        if callback_error is not None:
            if context_cleanup_error:
                raise callback_error from RuntimeError(context_cleanup_error)
            raise callback_error
        if cancellation is not None:
            cancelled_turn = cancellation.turn if isinstance(cancellation, _CodexTurnCancelled) else turn
            turn_id = cancellation.turn_id if isinstance(cancellation, _CodexTurnCancelled) else None
            cleanup_error = cancellation.cleanup_error if isinstance(cancellation, _CodexTurnCancelled) else None
            raise AgentCancelled(
                self._interrupted_result(
                    resumed_thread_id,
                    cancelled_turn,
                    notifications,
                    _join_errors(cleanup_error, context_cleanup_error),
                    turn_id,
                )
            )
        if runtime_error is not None:
            if context_cleanup_error:
                runtime_error = RuntimeError(
                    f"{str(runtime_error) or type(runtime_error).__name__}; " f"native cleanup: {context_cleanup_error}"
                )
            return self._failed_result(resumed_thread_id, runtime_error, notifications)
        return self._normalize_result(resumed_thread_id, turn, notifications)

    def _interrupted_result(
        self,
        thread_id: str | None,
        turn: Any | None,
        notifications: list[dict[str, Any]],
        cleanup_error: str | None,
        turn_id: str | None = None,
    ) -> AgentResult:
        error = _error_message(getattr(turn, "error", None)) or "Codex turn was cancelled."
        if cleanup_error:
            error = f"{error} Native cleanup: {cleanup_error}"
        if turn is not None:
            usage = _normalize_usage(getattr(turn, "usage", None))
            return AgentResult(
                thread_id=thread_id,
                status="interrupted",
                final_response=None,
                usage=usage,
                trace_jsonl=_trace_jsonl(
                    getattr(turn, "id", None) or turn_id,
                    "interrupted",
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
        usage = AgentUsage()
        return AgentResult(
            thread_id=thread_id,
            status="interrupted",
            final_response=None,
            usage=usage,
            trace_jsonl=_trace_jsonl(turn_id, "interrupted", (), usage, notifications),
            runtime_name="codex",
            runtime_version=self._runtime_version,
            model=self.model,
            model_provider=self.provider,
            duration_ms=None,
            error=error,
        )

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

    # openai-codex 0.144.4 starts turns in asyncio.to_thread(), so shielding keeps
    # the late turn handle reachable long enough to interrupt it after cancellation.
    start_task = asyncio.create_task(thread.turn(task, **kwargs))
    try:
        handle = await asyncio.shield(start_task)
    except asyncio.CancelledError:
        handle, start_error = await _wait_for_late_handle(start_task)
        if handle is None:
            raise _CodexTurnCancelled(None, start_error)
        turn, cleanup_error = await _interrupt_turn(handle, notifications)
        raise _CodexTurnCancelled(turn, _join_errors(start_error, cleanup_error), str(handle.id))

    collect_task = asyncio.create_task(_collect_thread_turn(handle, notifications))
    try:
        return await asyncio.shield(collect_task)
    except asyncio.CancelledError:
        turn, cleanup_error = await _interrupt_turn(handle, notifications, collect_task)
        raise _CodexTurnCancelled(turn, cleanup_error, str(handle.id))


async def _collect_thread_turn(handle: Any, notifications: list[dict[str, Any]]) -> Any:
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


async def _wait_for_late_handle(start_task: asyncio.Task[Any]) -> tuple[Any | None, str | None]:
    try:
        return await asyncio.wait_for(asyncio.shield(start_task), _LATE_TURN_START_SECONDS), None
    except asyncio.TimeoutError:
        # The underlying SDK thread cannot be cancelled; consume its eventual result
        # while the containment worker remains responsible for process-level cleanup.
        start_task.add_done_callback(_consume_task_result)
        return None, "turn handle was not available before the cancellation deadline"
    except Exception as exc:
        return None, str(exc) or "turn start failed during cancellation"


async def _interrupt_turn(
    handle: Any,
    notifications: list[dict[str, Any]],
    collect_task: asyncio.Task[Any] | None = None,
) -> tuple[Any | None, str | None]:
    errors: list[str] = []
    if collect_task is None:
        collect_task = asyncio.create_task(_collect_thread_turn(handle, notifications))
    try:
        await asyncio.wait_for(asyncio.shield(handle.interrupt()), _CANCEL_RPC_SECONDS)
    except Exception as exc:
        errors.append(f"interrupt failed: {str(exc) or type(exc).__name__}")

    try:
        turn = await asyncio.wait_for(asyncio.shield(collect_task), _CANCEL_DRAIN_SECONDS)
    except asyncio.TimeoutError:
        errors.append("interrupted turn did not reach a terminal event before the drain deadline")
        collect_task.cancel()
        await _consume_cancelled_task(collect_task)
        turn = None
    except Exception as exc:
        errors.append(f"interrupted turn drain failed: {str(exc) or type(exc).__name__}")
        turn = None
    return turn, "; ".join(errors) or None


async def _consume_cancelled_task(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except BaseException:
        pass


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


def _join_errors(*errors: str | None) -> str | None:
    return "; ".join(error for error in errors if error) or None


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
