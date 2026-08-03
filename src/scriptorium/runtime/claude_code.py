from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass, replace
from enum import Enum
import hashlib
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version as package_version
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ..domain import AgentRole
from .base import (
    CLAUDE_SDK_VERSION,
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

_READ_TOOLS = frozenset({"Read", "Glob", "Grep"})
_INTERRUPTED_REASONS = frozenset({"aborted_streaming", "aborted_tools"})
_CANCEL_RPC_SECONDS = 2.0
_CANCEL_DRAIN_SECONDS = 2.0


class ClaudeCodeAgentRuntime:
    def __init__(
        self,
        *,
        route: str,
        model: str,
        provider: str,
        reasoning: str,
        sdk_loader: Callable[[], Any] | None = None,
    ) -> None:
        self.route = route
        self.model = model
        self.provider = provider
        self.reasoning = reasoning

        try:
            sdk = (sdk_loader or (lambda: import_module("claude_agent_sdk")))()
            version = (
                str(package_version("claude-agent-sdk")) if sdk_loader is None else str(getattr(sdk, "__version__", ""))
            )
        except (ImportError, PackageNotFoundError) as exc:
            raise RuntimeUnavailable(
                f"The Claude Code runtime requires claude-agent-sdk=={CLAUDE_SDK_VERSION}. "
                "Install Scriptorium with the claude extra before running this route."
            ) from exc
        if version != CLAUDE_SDK_VERSION:
            raise RuntimeUnavailable(
                f"The Claude Code runtime requires claude-agent-sdk=={CLAUDE_SDK_VERSION}, "
                f"but found {version or 'an unknown version'}."
            )

        self._client_type = sdk.ClaudeSDKClient
        self._options_type = sdk.ClaudeAgentOptions
        self._hook_matcher_type = sdk.HookMatcher
        self._result_type = sdk.ResultMessage
        self._runtime_version = version
        self._copy_auth_files = (
            getattr(import_module("claude_agent_sdk._internal.session_resume"), "_copy_auth_files")
            if sdk_loader is None
            else sdk._copy_auth_files
        )

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
        resolved_workspace = workspace.resolve()
        resolved_session_dir = session_dir.resolve()
        resolved_session_dir.mkdir(parents=True, exist_ok=True)
        store = _FileSessionStore(resolved_session_dir)
        if thread_id is not None and not store.contains(thread_id):
            return self._failed_result(
                thread_id,
                RuntimeError(f"Claude session state is unavailable for {thread_id}."),
                (),
            )

        guard = _workspace_guard(resolved_workspace)
        with TemporaryDirectory(
            prefix="scriptorium-claude-",
            dir=resolved_session_dir,
            ignore_cleanup_errors=True,
        ) as native_dir:
            native_config_dir = Path(native_dir)
            self._copy_auth_files(native_config_dir, {})
            options = self._options_type(
                tools=sorted(_READ_TOOLS),
                allowed_tools=[],
                system_prompt=_role_instructions(role),
                mcp_servers={},
                strict_mcp_config=True,
                permission_mode="dontAsk",
                resume=thread_id,
                disallowed_tools=[],
                model=self.model,
                fallback_model=None,
                cwd=resolved_workspace,
                settings=None,
                add_dirs=[],
                hooks={
                    "PreToolUse": [
                        self._hook_matcher_type(
                            matcher=None,
                            hooks=[guard],
                        )
                    ]
                },
                agents=None,
                setting_sources=[],
                skills=[],
                plugins=[],
                effort=self.reasoning,
                output_format={"type": "json_schema", "schema": dict(schema)},
                env={"CLAUDE_CONFIG_DIR": str(native_config_dir)},
                session_store=store,
                session_store_flush="eager",
            )

            messages: list[Any] = []
            result_message: Any = None
            current_thread_id = thread_id
            # Claude 0.2.128's top-level query() cannot interrupt a live turn;
            # the public persistent client provides interrupt and ordered disconnect.
            client = self._client_type(options=options)
            receive_task: asyncio.Task[Any] | None = None
            connected = False
            cancelled = False
            callback_error: Exception | None = None
            runtime_error: Exception | None = None
            cleanup_errors: list[str] = []
            try:
                await client.connect()
                connected = True
                await client.query(task)
                receive_task = asyncio.create_task(
                    _receive_claude_response(
                        client,
                        messages,
                        self._result_type,
                        on_session_started,
                    )
                )
                try:
                    result_message, current_thread_id = await asyncio.shield(receive_task)
                    current_thread_id = current_thread_id or thread_id
                except _SessionStartedCallbackError as exc:
                    callback_error = exc.error
                    _, _, cleanup_error = await _interrupt_claude(client, receive_task)
                    if cleanup_error:
                        cleanup_errors.append(cleanup_error)
                except asyncio.CancelledError:
                    cancelled = True
                    drained_result, drained_thread_id, cleanup_error = await _interrupt_claude(client, receive_task)
                    result_message = drained_result
                    current_thread_id = drained_thread_id or _latest_session_id(messages) or thread_id
                    if cleanup_error:
                        cleanup_errors.append(cleanup_error)
            except asyncio.CancelledError:
                cancelled = True
                if connected:
                    drained_result, drained_thread_id, cleanup_error = await _interrupt_claude(client, receive_task)
                    result_message = drained_result
                    current_thread_id = drained_thread_id or _latest_session_id(messages) or thread_id
                    if cleanup_error:
                        cleanup_errors.append(cleanup_error)
            except _SessionStartedCallbackError as exc:
                callback_error = exc.error
                if connected:
                    _, _, cleanup_error = await _interrupt_claude(client, receive_task)
                    if cleanup_error:
                        cleanup_errors.append(cleanup_error)
            except Exception as exc:
                runtime_error = exc
            finally:
                try:
                    await client.disconnect()
                except asyncio.CancelledError:
                    cancelled = True
                    cleanup_errors.append("disconnect was cancelled")
                except Exception as exc:
                    cleanup_errors.append(f"disconnect failed: {str(exc) or type(exc).__name__}")

            current_thread_id = _latest_session_id(messages) or current_thread_id
            if callback_error is not None:
                raise callback_error
            if cancelled:
                raise AgentCancelled(
                    self._interrupted_result(
                        current_thread_id,
                        result_message,
                        messages,
                        "; ".join(cleanup_errors) or None,
                    )
                )
            if runtime_error is not None:
                if cleanup_errors:
                    runtime_error = RuntimeError(
                        f"{str(runtime_error) or type(runtime_error).__name__}; "
                        f"native cleanup: {'; '.join(cleanup_errors)}"
                    )
                return self._failed_result(current_thread_id, runtime_error, messages)
            if cleanup_errors:
                return self._failed_result(current_thread_id, RuntimeError("; ".join(cleanup_errors)), messages)

        if result_message is None:
            return self._failed_result(
                current_thread_id,
                RuntimeError("Claude result message not received."),
                messages,
            )
        result = self._normalize_result(result_message, messages)
        mirror_failed = any(getattr(message, "subtype", None) == "mirror_error" for message in messages)
        session_persisted = result.thread_id is not None and store.contains(result.thread_id)
        if result.status == "completed" and (mirror_failed or not session_persisted):
            return replace(
                result,
                status="failed",
                final_response=None,
                trace_jsonl=_trace_jsonl(messages, "failed", result.usage),
                error="Claude session state could not be persisted.",
            )
        return result

    def _interrupted_result(
        self,
        thread_id: str | None,
        result: Any,
        messages: list[Any],
        cleanup_error: str | None,
    ) -> AgentResult:
        usage = _normalize_usage(getattr(result, "usage", None))
        error = _result_error(result, "interrupted") if result is not None else "Claude turn was cancelled."
        if cleanup_error:
            error = f"{error} Native cleanup: {cleanup_error}"
        return AgentResult(
            thread_id=_optional_string(getattr(result, "session_id", None)) or thread_id,
            status="interrupted",
            final_response=None,
            usage=usage,
            trace_jsonl=_trace_jsonl(messages, "interrupted", usage),
            runtime_name="claude_code",
            runtime_version=self._runtime_version,
            model=self.model,
            model_provider=self.provider,
            duration_ms=_optional_int(getattr(result, "duration_ms", None)),
            error=error,
        )

    def _normalize_result(self, result: Any, messages: list[Any]) -> AgentResult:
        thread_id = _optional_string(getattr(result, "session_id", None))
        status = _normalize_status(result)
        usage = _normalize_usage(getattr(result, "usage", None))
        structured_output = getattr(result, "structured_output", None)
        error = _result_error(result, status)
        final_response: str | None = None
        if status == "completed":
            if structured_output is None:
                status = "failed"
                error = "Claude returned no structured output."
            else:
                final_response = _canonical_json(structured_output)

        return AgentResult(
            thread_id=thread_id,
            status=status,
            final_response=final_response,
            usage=usage,
            trace_jsonl=_trace_jsonl(messages, status, usage),
            runtime_name="claude_code",
            runtime_version=self._runtime_version,
            model=self.model,
            model_provider=self.provider,
            duration_ms=_optional_int(getattr(result, "duration_ms", None)),
            error=error,
        )

    def _failed_result(
        self,
        thread_id: str | None,
        exc: Exception,
        messages: list[Any] | tuple[Any, ...],
    ) -> AgentResult:
        usage = AgentUsage()
        return AgentResult(
            thread_id=thread_id,
            status="failed",
            final_response=None,
            usage=usage,
            trace_jsonl=_trace_jsonl(messages, "failed", usage),
            runtime_name="claude_code",
            runtime_version=self._runtime_version,
            model=self.model,
            model_provider=self.provider,
            duration_ms=None,
            error=str(exc) or "Claude Code runtime failed.",
        )


async def _receive_claude_response(
    client: Any,
    messages: list[Any],
    result_type: type[Any],
    on_session_started: SessionStartedCallback | None,
) -> tuple[Any, str | None]:
    result_message: Any = None
    current_thread_id: str | None = None
    reported_thread_id: str | None = None
    async for message in client.receive_response():
        messages.append(message)
        message_thread_id = _message_session_id(message)
        if message_thread_id is not None:
            current_thread_id = message_thread_id
            if message_thread_id != reported_thread_id:
                await _notify_session_started(on_session_started, message_thread_id)
                reported_thread_id = message_thread_id
        if isinstance(message, result_type):
            result_message = message
    return result_message, current_thread_id


async def _interrupt_claude(
    client: Any,
    receive_task: asyncio.Task[Any] | None,
) -> tuple[Any, str | None, str | None]:
    errors: list[str] = []
    try:
        await asyncio.wait_for(asyncio.shield(client.interrupt()), _CANCEL_RPC_SECONDS)
    except asyncio.CancelledError:
        errors.append("interrupt request was cancelled")
    except Exception as exc:
        errors.append(f"interrupt failed: {str(exc) or type(exc).__name__}")

    result_message: Any = None
    thread_id: str | None = None
    if receive_task is not None:
        try:
            result_message, thread_id = await asyncio.wait_for(
                asyncio.shield(receive_task),
                _CANCEL_DRAIN_SECONDS,
            )
        except _SessionStartedCallbackError:
            pass
        except asyncio.CancelledError:
            errors.append("interrupted response drain was cancelled")
        except asyncio.TimeoutError:
            errors.append("interrupted response did not finish before the drain deadline")
            receive_task.cancel()
            await _consume_cancelled_task(receive_task)
        except Exception as exc:
            errors.append(f"interrupted response drain failed: {str(exc) or type(exc).__name__}")
    return result_message, thread_id, "; ".join(errors) or None


async def _consume_cancelled_task(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except BaseException:
        pass


def _latest_session_id(messages: list[Any]) -> str | None:
    for message in reversed(messages):
        thread_id = _message_session_id(message)
        if thread_id is not None:
            return thread_id
    return None


class _FileSessionStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    async def append(self, key: Mapping[str, Any], entries: list[dict[str, Any]]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        seen = self._entry_ids(path)
        with path.open("a", encoding="utf-8") as handle:
            for entry in entries:
                entry_id = entry.get("uuid")
                if entry_id is not None and str(entry_id) in seen:
                    continue
                handle.write(_canonical_json(entry))
                handle.write("\n")
                if entry_id is not None:
                    seen.add(str(entry_id))

    async def load(self, key: Mapping[str, Any]) -> list[dict[str, Any]] | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def contains(self, session_id: str) -> bool:
        path = self._path({"session_id": session_id})
        return path.is_file() and path.stat().st_size > 0

    def _path(self, key: Mapping[str, Any]) -> Path:
        session_id = str(key["session_id"])
        subpath = str(key.get("subpath", ""))
        digest = hashlib.sha256(f"{session_id}\0{subpath}".encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.jsonl"

    @staticmethod
    def _entry_ids(path: Path) -> set[str]:
        if not path.is_file():
            return set()
        return {
            str(entry["uuid"])
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
            for entry in (json.loads(line),)
            if entry.get("uuid") is not None
        }


def _workspace_guard(workspace: Path) -> Callable[[Mapping[str, Any], str | None, Mapping[str, Any]], Any]:
    async def guard(
        input_data: Mapping[str, Any],
        _tool_use_id: str | None,
        _context: Mapping[str, Any],
    ) -> dict[str, Any]:
        tool_name = str(input_data.get("tool_name", ""))
        tool_input = input_data.get("tool_input")
        if tool_name not in _READ_TOOLS or not isinstance(tool_input, Mapping):
            return _deny("Only the Read, Glob, and Grep tools are available.")

        if tool_name == "Read":
            raw_path = tool_input.get("file_path")
            if not isinstance(raw_path, str) or not raw_path:
                return _deny("Read requires a workspace-relative file path.")
            if not _inside_workspace(raw_path, workspace):
                return _deny("Reading outside the task workspace is not allowed.")
        else:
            raw_path = tool_input.get("path")
            if raw_path is not None and (not isinstance(raw_path, str) or not _inside_workspace(raw_path, workspace)):
                return _deny("Searching outside the task workspace is not allowed.")

        if tool_name == "Glob":
            pattern = tool_input.get("pattern")
            if not isinstance(pattern, str) or _escaping_glob(pattern):
                return _deny("Glob patterns must stay within the task workspace.")

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "Read-only access is confined to the task workspace.",
            }
        }

    return guard


def _inside_workspace(raw_path: str, workspace: Path) -> bool:
    try:
        path = Path(raw_path).expanduser()
        candidate = path if path.is_absolute() else workspace / path
        return candidate.resolve().is_relative_to(workspace)
    except (OSError, RuntimeError, ValueError):
        return False


def _escaping_glob(pattern: str) -> bool:
    path = Path(pattern).expanduser()
    return path.is_absolute() or ".." in path.parts


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _normalize_status(result: Any) -> AgentStatus:
    terminal_reason = _optional_string(getattr(result, "terminal_reason", None))
    if terminal_reason in _INTERRUPTED_REASONS:
        return "interrupted"
    if not bool(getattr(result, "is_error", True)) and getattr(result, "subtype", None) == "success":
        return "completed"
    return "failed"


def _normalize_usage(value: Any) -> AgentUsage:
    if not isinstance(value, Mapping):
        return AgentUsage()
    regular = _token_count(value, "input_tokens", "inputTokens")
    cache_creation = _token_count(value, "cache_creation_input_tokens", "cacheCreationInputTokens")
    cache_read = _token_count(value, "cache_read_input_tokens", "cacheReadInputTokens")
    return AgentUsage(
        input_tokens=regular + cache_creation + cache_read,
        cached_input_tokens=cache_read,
        output_tokens=_token_count(value, "output_tokens", "outputTokens"),
        reasoning_tokens=0,
    )


def _token_count(value: Mapping[str, Any], *names: str) -> int:
    for name in names:
        if name in value and value[name] is not None:
            return int(value[name])
    return 0


def _result_error(result: Any, status: AgentStatus) -> str | None:
    if status == "completed":
        return None
    errors = getattr(result, "errors", None)
    if errors:
        return "; ".join(str(error) for error in errors)
    text = _optional_string(getattr(result, "result", None))
    if text:
        return text
    terminal_reason = _optional_string(getattr(result, "terminal_reason", None))
    if terminal_reason:
        return f"Claude turn ended with {terminal_reason}."
    return "Claude Code runtime failed."


def _message_session_id(message: Any) -> str | None:
    session_id = _optional_string(getattr(message, "session_id", None))
    if session_id is not None:
        return session_id
    data = getattr(message, "data", None)
    if isinstance(data, Mapping):
        return _optional_string(data.get("session_id"))
    return None


def _trace_jsonl(messages: Any, status: AgentStatus, usage: AgentUsage) -> str:
    records = [{"kind": "message", "message": _to_plain(message)} for message in messages]
    records.append({"kind": "normalized_result", "status": status, "usage": asdict(usage)})
    return "\n".join(_canonical_json(record) for record in records)


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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
