from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass, replace
from enum import Enum
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path
import time
from typing import Any

from ..domain import AgentRole, canonical_json
from .base import (
    ANTIGRAVITY_SDK_VERSION,
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

_RUNTIME_NAME = "antigravity"


class AntigravityAgentRuntime:
    def __init__(
        self,
        *,
        route: str,
        model: str,
        provider: str,
        reasoning: str,
        sdk: Any | None = None,
    ) -> None:
        self.route = route
        self.model = model
        self.provider = provider
        self.reasoning = reasoning

        if sdk is None:
            try:
                sdk = import_module("google.antigravity")
                sdk_types = import_module("google.antigravity.types")
                installed_version = package_version("google-antigravity")
            except (ImportError, PackageNotFoundError) as exc:
                raise RuntimeUnavailable(
                    f"The Antigravity runtime requires google-antigravity=={ANTIGRAVITY_SDK_VERSION}. "
                    "Install Scriptorium with the antigravity extra before running this route."
                ) from exc
            if installed_version != ANTIGRAVITY_SDK_VERSION:
                raise RuntimeUnavailable(
                    f"The Antigravity runtime requires google-antigravity=={ANTIGRAVITY_SDK_VERSION}, "
                    f"but found {installed_version}."
                )
            runtime_version = installed_version
        else:
            sdk_types = sdk.types
            runtime_version = "injected"

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeUnavailable("The Antigravity runtime requires GEMINI_API_KEY for Gemini API authentication.")
        try:
            thinking_level = sdk_types.ThinkingLevel(reasoning)
        except ValueError as exc:
            supported = ", ".join(level.value for level in sdk_types.ThinkingLevel)
            raise RuntimeUnavailable(
                f"The Antigravity runtime does not support reasoning_effort {reasoning!r}; "
                f"expected one of: {supported}."
            ) from exc

        self._sdk = sdk
        self._types = sdk_types
        self._api_key = api_key
        self._thinking_level = thinking_level
        self._runtime_version = runtime_version

    async def run_agent(
        self,
        task: str,
        role: AgentRole,
        workspace: Path,
        schema: Mapping[str, object],
        session_dir: Path,
        on_session_started: SessionStartedCallback | None = None,
    ) -> AgentResult:
        return await self._run(
            thread_id=None,
            task=task,
            role=role,
            workspace=workspace,
            schema=schema,
            session_dir=session_dir,
            on_session_started=on_session_started,
        )

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
        return await self._run(
            thread_id=thread_id,
            task=task,
            role=role,
            workspace=workspace,
            schema=schema,
            session_dir=session_dir,
            on_session_started=on_session_started,
        )

    async def _run(
        self,
        *,
        thread_id: str | None,
        task: str,
        role: AgentRole,
        workspace: Path,
        schema: Mapping[str, object],
        session_dir: Path,
        on_session_started: SessionStartedCallback | None,
    ) -> AgentResult:
        started = time.monotonic()
        resolved_session_dir = session_dir.resolve()
        save_dir = resolved_session_dir / "save"
        app_data_dir = resolved_session_dir / "app"
        save_dir.mkdir(parents=True, exist_ok=True)
        app_data_dir.mkdir(parents=True, exist_ok=True)

        config_kwargs: dict[str, Any] = {
            "system_instructions": _role_instructions(role),
            "capabilities": self._types.CapabilitiesConfig(
                enabled_tools=[
                    self._types.BuiltinTools.LIST_DIR,
                    self._types.BuiltinTools.SEARCH_DIR,
                    self._types.BuiltinTools.FIND_FILE,
                    self._types.BuiltinTools.VIEW_FILE,
                    self._types.BuiltinTools.FINISH,
                ],
                enable_subagents=False,
            ),
            "tools": [],
            "policies": [],
            "hooks": [],
            "triggers": [],
            "mcp_servers": [],
            "subagents": [],
            "skills_paths": [],
            "workspaces": [str(workspace.resolve())],
            "save_dir": str(save_dir),
            "app_data_dir": str(app_data_dir),
            "response_schema": dict(schema),
            "model": self._types.ModelTarget(
                name=self.model,
                types=[self._types.ModelType.TEXT],
                endpoint=self._types.GeminiAPIEndpoint(
                    api_key=self._api_key,
                    options=self._types.GeminiModelOptions(thinking_level=self._thinking_level),
                ),
            ),
            "api_key": self._api_key,
            "vertex": False,
        }
        if thread_id is not None:
            config_kwargs.update(
                {
                    "conversation_id": thread_id,
                    "session_continuation_mode": self._types.SessionContinuationMode.RESUME,
                }
            )

        current_thread_id = thread_id
        agent: Any = None
        response: Any = None
        steps: list[Any] = []
        usage_value: Any = None
        structured_output: Any = None
        reported_thread_id: str | None = None
        interrupted: AgentResult | None = None
        external_cancelled = False
        context_cleanup_error: str | None = None
        history_start = 0
        try:
            config = self._sdk.LocalAgentConfig(**config_kwargs)
            async with self._sdk.Agent(config) as agent:
                history_start = len(agent.conversation.history)
                try:
                    current_thread_id = _optional_string(agent.conversation_id) or current_thread_id
                    if current_thread_id is not None:
                        await _notify_session_started(on_session_started, current_thread_id)
                        reported_thread_id = current_thread_id
                    response = await agent.chat(task)
                    structured_output = await response.structured_output()
                    usage_value = response.usage_metadata
                    steps = agent.conversation.history[history_start:]
                    current_thread_id = _optional_string(agent.conversation_id) or current_thread_id
                    if current_thread_id is not None and current_thread_id != reported_thread_id:
                        await _notify_session_started(on_session_started, current_thread_id)
                        reported_thread_id = current_thread_id
                except asyncio.CancelledError as exc:
                    cleanup_error = await _cancel_active(agent)
                    steps = agent.conversation.history[history_start:]
                    current_thread_id = _optional_string(agent.conversation_id) or current_thread_id
                    if current_thread_id is not None and current_thread_id != reported_thread_id:
                        await _notify_session_started(on_session_started, current_thread_id)
                        reported_thread_id = current_thread_id
                    error = str(exc) or "Antigravity turn was cancelled."
                    if cleanup_error:
                        error = f"{error} Native cleanup: {cleanup_error}"
                    interrupted = self._result(
                        thread_id=current_thread_id,
                        status="interrupted",
                        structured_output=None,
                        usage_value=getattr(response, "usage_metadata", None),
                        steps=steps,
                        duration_ms=_duration_ms(started),
                        error=error,
                    )
                    external_cancelled = not isinstance(exc, self._types.AntigravityCancelledError)
                except Exception:
                    steps = agent.conversation.history[history_start:]
                    current_thread_id = _optional_string(agent.conversation_id) or current_thread_id
                    usage_value = getattr(response, "usage_metadata", None)
                    raise
        except _SessionStartedCallbackError as exc:
            raise exc.error
        except asyncio.CancelledError as exc:
            if interrupted is None:
                cleanup_error = await _cancel_active(agent)
                if agent is not None:
                    steps = agent.conversation.history[history_start:]
                    current_thread_id = _optional_string(agent.conversation_id) or current_thread_id
                error = str(exc) or "Antigravity turn was cancelled."
                if cleanup_error:
                    error = f"{error} Native cleanup: {cleanup_error}"
                interrupted = self._result(
                    thread_id=current_thread_id,
                    status="interrupted",
                    structured_output=None,
                    usage_value=usage_value,
                    steps=steps,
                    duration_ms=_duration_ms(started),
                    error=error,
                )
                external_cancelled = True
            else:
                context_cleanup_error = str(exc) or "Antigravity agent cleanup was cancelled"
        except Exception as exc:
            if interrupted is not None:
                context_cleanup_error = str(exc) or type(exc).__name__
            # Python 3.10 has no Task.cancelling(); cleanup failures retain the injected cancellation as context.
            elif isinstance(exc.__context__, asyncio.CancelledError):
                cleanup_error = await _cancel_active(agent)
                if agent is not None:
                    steps = agent.conversation.history[history_start:]
                    current_thread_id = _optional_string(agent.conversation_id) or current_thread_id
                error = "Antigravity turn was cancelled."
                if cleanup_error:
                    error = f"{error} Native cleanup: {cleanup_error}"
                interrupted = self._result(
                    thread_id=current_thread_id,
                    status="interrupted",
                    structured_output=None,
                    usage_value=usage_value,
                    steps=steps,
                    duration_ms=_duration_ms(started),
                    error=error,
                )
                external_cancelled = True
                context_cleanup_error = str(exc) or type(exc).__name__
            else:
                return self._result(
                    thread_id=current_thread_id,
                    status="failed",
                    structured_output=None,
                    usage_value=usage_value,
                    steps=steps,
                    duration_ms=_duration_ms(started),
                    error=str(exc) or "Antigravity runtime failed.",
                )

        if interrupted is not None:
            if context_cleanup_error:
                interrupted = replace(
                    interrupted,
                    error=(
                        f"{interrupted.error} Native cleanup: {context_cleanup_error}"
                        if interrupted.error
                        else f"Native cleanup: {context_cleanup_error}"
                    ),
                )
            if external_cancelled:
                raise AgentCancelled(interrupted)
            return interrupted

        status, error = _turn_status(steps, structured_output)
        return self._result(
            thread_id=current_thread_id,
            status=status,
            structured_output=structured_output,
            usage_value=usage_value,
            steps=steps,
            duration_ms=_duration_ms(started),
            error=error,
        )

    def _result(
        self,
        *,
        thread_id: str | None,
        status: AgentStatus,
        structured_output: Any,
        usage_value: Any,
        steps: list[Any],
        duration_ms: int,
        error: str | None,
    ) -> AgentResult:
        usage = _normalize_usage(usage_value)
        return AgentResult(
            thread_id=thread_id,
            status=status,
            final_response=canonical_json(structured_output) if status == "completed" else None,
            usage=usage,
            trace_jsonl=_trace_jsonl(steps, usage_value, status, usage),
            runtime_name=_RUNTIME_NAME,
            runtime_version=self._runtime_version,
            model=self.model,
            model_provider=self.provider,
            duration_ms=duration_ms,
            error=error,
        )


async def _cancel_active(agent: Any) -> str | None:
    try:
        if agent is not None and agent.is_started:
            # google-antigravity 0.1.8 may mark the response done before external
            # cancellation, making response.cancel() a no-op; conversation owns the turn.
            await agent.conversation.cancel()
    except asyncio.CancelledError as exc:
        return str(exc) or "conversation cancellation was interrupted"
    except Exception as exc:
        return str(exc) or type(exc).__name__
    return None


def _turn_status(steps: list[Any], structured_output: Any) -> tuple[AgentStatus, str | None]:
    for step in reversed(steps):
        step_type = getattr(getattr(step, "type", None), "value", getattr(step, "type", None))
        if step_type != "FINISH":
            continue
        status = getattr(getattr(step, "status", None), "value", getattr(step, "status", None))
        if status in {"CANCELED", "CANCELLED"}:
            return "interrupted", _step_error(step) or "Antigravity turn was cancelled."
        if status == "ERROR":
            return "failed", _step_error(step) or "Antigravity turn failed."
        if status == "DONE" and structured_output is not None:
            return "completed", None
        break
    if structured_output is None:
        return "failed", "Antigravity turn did not return structured output."
    return "failed", "Antigravity turn did not complete with a successful FINISH step."


def _step_error(step: Any) -> str | None:
    value = getattr(step, "error", None)
    return None if not value else str(value)


def _normalize_usage(value: Any) -> AgentUsage:
    if value is None:
        return AgentUsage()
    prompt = _token_count(value, "prompt_token_count")
    cached = _token_count(value, "cached_content_token_count")
    candidates = _token_count(value, "candidates_token_count")
    thoughts = _token_count(value, "thoughts_token_count")
    return AgentUsage(
        input_tokens=prompt,
        cached_input_tokens=cached,
        output_tokens=candidates + thoughts,
        reasoning_tokens=thoughts,
    )


def _token_count(value: Any, name: str) -> int:
    if isinstance(value, Mapping):
        raw = value.get(name)
    else:
        raw = getattr(value, name, None)
    return 0 if raw is None else int(raw)


def _trace_jsonl(
    steps: list[Any],
    usage_value: Any,
    status: AgentStatus,
    usage: AgentUsage,
) -> str:
    records = [{"kind": "step", "step": _to_plain(step)} for step in steps]
    if usage_value is not None:
        records.append({"kind": "provider_usage", "usage": _to_plain(usage_value)})
    records.append({"kind": "normalized_result", "status": status, "usage": asdict(usage)})
    return "\n".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) for record in records
    )


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


def _duration_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)
