from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from scriptorium.runtime.antigravity import AntigravityAgentRuntime


class FakeBuiltinTools(str, Enum):
    LIST_DIR = "list_directory"
    SEARCH_DIR = "search_directory"
    FIND_FILE = "find_file"
    VIEW_FILE = "view_file"
    CREATE_FILE = "create_file"
    EDIT_FILE = "edit_file"
    RUN_COMMAND = "run_command"
    ASK_QUESTION = "ask_question"
    START_SUBAGENT = "start_subagent"
    SEARCH_WEB = "search_web"
    READ_URL_CONTENT = "read_url_content"
    FINISH = "finish"


class FakeSessionContinuationMode(str, Enum):
    RESUME = "resume"
    CREATE_OR_RESUME = "create_or_resume"


class FakeThinkingLevel(str, Enum):
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTRA_HIGH = "extra_high"


class FakeModelType(str, Enum):
    TEXT = "text"
    IMAGE = "image"


class FakeAntigravityCancelledError(asyncio.CancelledError):
    pass


@dataclass
class FakeCapabilitiesConfig:
    enabled_tools: list[FakeBuiltinTools]
    enable_subagents: bool


@dataclass
class FakeGeminiModelOptions:
    thinking_level: FakeThinkingLevel


@dataclass
class FakeGeminiAPIEndpoint:
    api_key: str
    options: FakeGeminiModelOptions


@dataclass
class FakeModelTarget:
    name: str
    types: list[FakeModelType]
    endpoint: FakeGeminiAPIEndpoint


@dataclass
class FakeUsage:
    prompt_token_count: int = 37
    cached_content_token_count: int = 9
    candidates_token_count: int = 13
    thoughts_token_count: int = 5
    total_token_count: int = 55


@dataclass
class FakeStep:
    id: str
    status: str
    content: str = ""
    error: str = ""
    type: str = "UNKNOWN"


class FakeResponse:
    def __init__(
        self,
        *,
        structured_output: Any = None,
        usage: FakeUsage | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.output = structured_output
        self.usage_metadata = usage
        self.error = error
        self.cancelled = False

    async def structured_output(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.output

    async def cancel(self) -> None:
        self.cancelled = True


class FakeConversation:
    def __init__(
        self,
        history: list[FakeStep] | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        self.history = list(history or [])
        self.cancelled = False
        self.on_cancel = on_cancel

    async def cancel(self) -> None:
        self.cancelled = True
        if self.on_cancel is not None:
            self.on_cancel()


class FakeAgent:
    response: FakeResponse
    prior_history: list[FakeStep]
    current_steps: list[FakeStep]
    conversation_id_value: str | None
    cancelled_conversation_id_value: str | None
    exit_error_value: Exception | None
    exit_started_event: asyncio.Event | None
    exit_release_event: asyncio.Event | None
    instances: list["FakeAgent"] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self._conversation_id = self.conversation_id_value
        self.conversation = FakeConversation(self.prior_history, self._set_cancelled_conversation_id)
        self.is_started = False
        self.chat_calls: list[str] = []
        self.instances.append(self)

    async def __aenter__(self) -> "FakeAgent":
        self.is_started = True
        return self

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self.exit_started_event is not None:
            self.exit_started_event.set()
        try:
            if self.exit_release_event is not None:
                await self.exit_release_event.wait()
        except asyncio.CancelledError:
            self.is_started = False
            if self.exit_error_value is not None:
                raise self.exit_error_value
            raise
        self.is_started = False
        if self.exit_error_value is not None:
            raise self.exit_error_value

    async def chat(self, task: str) -> FakeResponse:
        self.chat_calls.append(task)
        self.conversation.history.extend(self.current_steps)
        return self.response

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id

    def _set_cancelled_conversation_id(self) -> None:
        if self.cancelled_conversation_id_value is not None:
            self._conversation_id = self.cancelled_conversation_id_value


class FakeLocalAgentConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def make_sdk(
    *,
    response: FakeResponse,
    current_steps: list[FakeStep],
    prior_history: list[FakeStep] | None = None,
    conversation_id: str | None = "12345678-1234-1234-1234-123456789012",
    cancelled_conversation_id: str | None = None,
    exit_error: Exception | None = None,
) -> Any:
    FakeAgent.response = response
    FakeAgent.current_steps = current_steps
    FakeAgent.prior_history = list(prior_history or [])
    FakeAgent.conversation_id_value = conversation_id
    FakeAgent.cancelled_conversation_id_value = cancelled_conversation_id
    FakeAgent.exit_error_value = exit_error
    FakeAgent.exit_started_event = None
    FakeAgent.exit_release_event = None
    FakeAgent.instances = []
    types = SimpleNamespace(
        AntigravityCancelledError=FakeAntigravityCancelledError,
        BuiltinTools=FakeBuiltinTools,
        CapabilitiesConfig=FakeCapabilitiesConfig,
        GeminiAPIEndpoint=FakeGeminiAPIEndpoint,
        GeminiModelOptions=FakeGeminiModelOptions,
        ModelTarget=FakeModelTarget,
        ModelType=FakeModelType,
        SessionContinuationMode=FakeSessionContinuationMode,
        ThinkingLevel=FakeThinkingLevel,
    )
    return SimpleNamespace(
        Agent=FakeAgent,
        LocalAgentConfig=FakeLocalAgentConfig,
        types=types,
    )


def make_runtime(
    monkeypatch: pytest.MonkeyPatch,
    sdk: Any,
    *,
    reasoning: str = "high",
) -> AntigravityAgentRuntime:
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    return AntigravityAgentRuntime(
        route="gemini_review",
        model="gemini-test",
        provider="gemini",
        reasoning=reasoning,
        sdk=sdk,
    )
