from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scriptorium.runtime import CLAUDE_SDK_VERSION
from scriptorium.runtime.claude_code import ClaudeCodeAgentRuntime


@dataclass
class FakeHookMatcher:
    matcher: str | None
    hooks: list[Any]


class FakeOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


@dataclass
class FakeAssistantMessage:
    text: str
    session_id: str | None = None


@dataclass
class FakeResultMessage:
    subtype: str
    duration_ms: int
    is_error: bool
    session_id: str
    usage: dict[str, Any] | None
    structured_output: Any = None
    result: str | None = None
    errors: list[str] | None = None
    terminal_reason: str | None = "completed"
    total_cost_usd: float | None = None


@dataclass
class FakeMirrorErrorMessage:
    subtype: str = "mirror_error"
    error: str = "mirror failed"


class FakeQuery:
    def __init__(self, responses: list[list[Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, FakeOptions]] = []
        self.loaded_sessions: list[list[dict[str, Any]] | None] = []
        self.native_config_dirs: list[Path] = []
        self.native_configs_ready: list[bool] = []
        self.client_events: list[str] = []
        self.interrupt_calls = 0
        self.disconnect_calls = 0

    def __call__(self, *, prompt: str, options: FakeOptions):
        self.calls.append((prompt, options))
        native_config_dir = Path(options.env["CLAUDE_CONFIG_DIR"])
        self.native_config_dirs.append(native_config_dir)
        self.native_configs_ready.append(
            native_config_dir.is_dir() and (native_config_dir / ".credentials.json").is_file()
        )
        messages = self.responses.pop(0)

        async def stream():
            if options.resume is not None:
                self.loaded_sessions.append(
                    await options.session_store.load({"project_key": "test", "session_id": options.resume})
                )
            for index, message in enumerate(messages):
                session_id = getattr(message, "session_id", None)
                if session_id is not None:
                    await options.session_store.append(
                        {"project_key": "test", "session_id": session_id},
                        [
                            {
                                "type": "message",
                                "uuid": f"{session_id}-{len(self.calls)}-{index}",
                            }
                        ],
                    )
                yield message

        return stream()


class FixedStreamQuery:
    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.calls: list[tuple[str, FakeOptions]] = []
        self.client_events: list[str] = []
        self.interrupt_calls = 0
        self.disconnect_calls = 0

    def __call__(self, *, prompt: str, options: FakeOptions) -> Any:
        self.calls.append((prompt, options))
        return self.stream


def _sdk(query: Any, version: str = CLAUDE_SDK_VERSION) -> Any:
    class FakeClaudeSDKClient:
        def __init__(self, options: FakeOptions) -> None:
            self.options = options
            self.stream: Any = None

        async def connect(self) -> None:
            if hasattr(query, "client_events"):
                query.client_events.append("connect")

        async def query(self, prompt: str) -> None:
            if hasattr(query, "client_events"):
                query.client_events.append("query")
            self.stream = query(prompt=prompt, options=self.options)

        def receive_response(self):
            if hasattr(query, "client_events"):
                query.client_events.append("receive_response")
            return self.stream

        async def interrupt(self) -> None:
            if hasattr(query, "client_events"):
                query.client_events.append("interrupt")
                query.interrupt_calls += 1
            if hasattr(self.stream, "interrupt"):
                await self.stream.interrupt()

        async def disconnect(self) -> None:
            if hasattr(query, "client_events"):
                query.client_events.append("disconnect")
                query.disconnect_calls += 1
            if self.stream is not None and hasattr(self.stream, "aclose"):
                await self.stream.aclose()

    return type(
        "FakeSDK",
        (),
        {
            "__version__": version,
            "ClaudeSDKClient": FakeClaudeSDKClient,
            "ClaudeAgentOptions": FakeOptions,
            "HookMatcher": FakeHookMatcher,
            "ResultMessage": FakeResultMessage,
            "_copy_auth_files": staticmethod(_copy_auth_files),
        },
    )


def _copy_auth_files(directory: Path, _env: dict[str, str]) -> None:
    (directory / ".credentials.json").write_text("{}", encoding="utf-8")


def _runtime(query: Any) -> ClaudeCodeAgentRuntime:
    return ClaudeCodeAgentRuntime(
        route="claude_review",
        model="claude-test",
        provider="anthropic",
        reasoning="high",
        sdk_loader=lambda: _sdk(query),
    )


def _success(session_id: str = "11111111-1111-4111-8111-111111111111") -> FakeResultMessage:
    return FakeResultMessage(
        subtype="success",
        duration_ms=123,
        is_error=False,
        session_id=session_id,
        usage={
            "input_tokens": 10,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 5,
            "output_tokens": 7,
            "service_tier": "standard",
        },
        structured_output={"z": 2, "answer": "ok"},
        total_cost_usd=0.012,
    )


class RaisingQuery:
    def __call__(self, *, prompt: str, options: FakeOptions):
        async def stream():
            yield FakeAssistantMessage("started", "77777777-7777-4777-8777-777777777777")
            raise RuntimeError("transport failed")

        return stream()


class CancellingStream:
    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self) -> "CancellingStream":
        return self

    async def __anext__(self) -> Any:
        raise asyncio.CancelledError

    async def aclose(self) -> None:
        self.closed = True


class InterruptibleStream:
    def __init__(self, result: FakeResultMessage) -> None:
        self.result = result
        self.interrupted = asyncio.Event()
        self.closed = False
        self._yielded = False

    def __aiter__(self) -> "InterruptibleStream":
        return self

    async def __anext__(self) -> Any:
        if self._yielded:
            raise StopAsyncIteration
        await self.interrupted.wait()
        self._yielded = True
        return self.result

    async def interrupt(self) -> None:
        self.interrupted.set()

    async def aclose(self) -> None:
        self.closed = True
