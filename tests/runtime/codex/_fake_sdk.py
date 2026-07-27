from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace
from typing import Any

from scriptorium.runtime.codex import CodexAgentRuntime


class FakeStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass
class FakeItem:
    kind: str
    text: str


class FakeThread:
    def __init__(self, thread_id: str, result: Any = None, error: Exception | None = None) -> None:
        self.id = thread_id
        self.result = result
        self.error = error
        self.run_calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, task: str, **kwargs: Any) -> Any:
        self.run_calls.append((task, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


class FakeStreamingThread:
    def __init__(self) -> None:
        self.id = "thread_stream"
        self.turn_calls: list[tuple[str, dict[str, Any]]] = []

    async def turn(self, task: str, **kwargs: Any) -> Any:
        self.turn_calls.append((task, kwargs))
        usage = make_result().usage
        item = SimpleNamespace(type="agentMessage", phase=SimpleNamespace(value="final_answer"), text='{"ok":true}')
        completed = SimpleNamespace(
            id="turn_stream",
            status=FakeStatus.COMPLETED,
            error=None,
            duration_ms=9,
            items=[],
        )
        notifications = [
            SimpleNamespace(
                method="item/completed",
                payload=SimpleNamespace(turn_id="turn_stream", item=item),
            ),
            SimpleNamespace(
                method="thread/tokenUsage/updated",
                payload=SimpleNamespace(turn_id="turn_stream", token_usage=usage),
            ),
            SimpleNamespace(
                method="turn/completed",
                payload=SimpleNamespace(turn=completed),
            ),
        ]
        return SimpleNamespace(id="turn_stream", stream=lambda: _notification_stream(notifications))


class FakeClient:
    def __init__(self, start_thread: FakeThread, resume_thread: FakeThread | None = None) -> None:
        self.start_thread = start_thread
        self.resume_thread = resume_thread or start_thread
        self.start_calls: list[dict[str, Any]] = []
        self.resume_calls: list[tuple[str, dict[str, Any]]] = []
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> "FakeClient":
        self.entered = True
        return self

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.exited = True

    async def thread_start(self, **kwargs: Any) -> FakeThread:
        self.start_calls.append(kwargs)
        return self.start_thread

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> FakeThread:
        self.resume_calls.append((thread_id, kwargs))
        return self.resume_thread


def make_runtime(client: FakeClient) -> CodexAgentRuntime:
    return CodexAgentRuntime(
        route="primary",
        model="test-model",
        provider="test-provider",
        reasoning="high",
        client_factory=lambda: client,
    )


async def _notification_stream(notifications: list[Any]):
    for notification in notifications:
        yield notification


def make_result(status: FakeStatus = FakeStatus.COMPLETED) -> SimpleNamespace:
    usage = SimpleNamespace(
        last=SimpleNamespace(
            input_tokens=31,
            cached_input_tokens=7,
            output_tokens=11,
            reasoning_output_tokens=5,
            total_tokens=42,
        ),
        total=SimpleNamespace(
            input_tokens=99,
            cached_input_tokens=20,
            output_tokens=30,
            reasoning_output_tokens=12,
            total_tokens=129,
        ),
    )
    error = SimpleNamespace(message="model failed") if status is FakeStatus.FAILED else None
    return SimpleNamespace(
        id="turn_1",
        status=status,
        error=error,
        duration_ms=123,
        final_response='{"summary":"ok"}',
        items=[FakeItem(kind="agentMessage", text="done")],
        usage=usage,
    )
