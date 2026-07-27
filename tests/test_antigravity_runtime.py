from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scriptorium.antigravity_runtime import ANTIGRAVITY_SDK_VERSION, AntigravityAgentRuntime
from scriptorium.domain import AgentRole
from scriptorium.runtime import AgentResult, AgentUsage, RuntimeUnavailable


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


class FakeAntigravityCancelledError(asyncio.CancelledError):
    pass


@dataclass
class FakeCapabilitiesConfig:
    enabled_tools: list[FakeBuiltinTools]
    enable_subagents: bool


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
    def __init__(self, history: list[FakeStep] | None = None) -> None:
        self.history = list(history or [])
        self.cancelled = False

    async def cancel(self) -> None:
        self.cancelled = True


class FakeAgent:
    response: FakeResponse
    prior_history: list[FakeStep]
    current_steps: list[FakeStep]
    conversation_id_value: str
    instances: list["FakeAgent"] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.conversation = FakeConversation(self.prior_history)
        self.is_started = False
        self.chat_calls: list[str] = []
        self.instances.append(self)

    async def __aenter__(self) -> "FakeAgent":
        self.is_started = True
        return self

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.is_started = False

    async def chat(self, task: str) -> FakeResponse:
        self.chat_calls.append(task)
        self.conversation.history.extend(self.current_steps)
        return self.response

    @property
    def conversation_id(self) -> str:
        return self.conversation_id_value


class FakeLocalAgentConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def make_sdk(
    *,
    response: FakeResponse,
    current_steps: list[FakeStep],
    prior_history: list[FakeStep] | None = None,
    conversation_id: str = "12345678-1234-1234-1234-123456789012",
) -> Any:
    FakeAgent.response = response
    FakeAgent.current_steps = current_steps
    FakeAgent.prior_history = list(prior_history or [])
    FakeAgent.conversation_id_value = conversation_id
    FakeAgent.instances = []
    types = SimpleNamespace(
        AntigravityCancelledError=FakeAntigravityCancelledError,
        BuiltinTools=FakeBuiltinTools,
        CapabilitiesConfig=FakeCapabilitiesConfig,
        SessionContinuationMode=FakeSessionContinuationMode,
    )
    return SimpleNamespace(
        Agent=FakeAgent,
        LocalAgentConfig=FakeLocalAgentConfig,
        types=types,
    )


def make_runtime(monkeypatch: pytest.MonkeyPatch, sdk: Any) -> AntigravityAgentRuntime:
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    return AntigravityAgentRuntime(
        route="gemini_review",
        model="gemini-test",
        provider="gemini",
        reasoning="high",
        sdk=sdk,
    )


def test_run_agent_uses_gemini_read_only_config_and_normalizes_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = [FakeStep("old", "DONE", "old turn")]
    current = [FakeStep("new", "DONE", "new turn")]
    sdk = make_sdk(
        response=FakeResponse(structured_output={"summary": "ok"}, usage=FakeUsage()),
        current_steps=current,
        prior_history=prior,
    )
    runtime = make_runtime(monkeypatch, sdk)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_dir = tmp_path / "state"
    schema = {"type": "object", "required": ["summary"]}

    result = asyncio.run(
        runtime.run_agent(
            "Review the manuscript.",
            AgentRole.SUBSTANTIVE_REVIEW,
            workspace,
            schema,
            session_dir,
        )
    )

    assert result == AgentResult(
        thread_id="12345678-1234-1234-1234-123456789012",
        status="completed",
        final_response='{"summary":"ok"}',
        usage=AgentUsage(input_tokens=37, cached_input_tokens=9, output_tokens=18, reasoning_tokens=5),
        trace_jsonl=result.trace_jsonl,
        runtime_name="antigravity",
        runtime_version="injected",
        model="gemini-test",
        model_provider="gemini",
        duration_ms=result.duration_ms,
        error=None,
    )
    assert result.duration_ms is not None
    assert (session_dir / "save").is_dir()
    assert (session_dir / "app").is_dir()

    agent = FakeAgent.instances[0]
    assert agent.chat_calls == ["Review the manuscript."]
    config = agent.config.kwargs
    assert config["system_instructions"] == (
        "You are Scholiast, the Scriptorium substantive_review agent. "
        "Claims, evidence, methods, and scientific review. Work read-only and do not modify files. "
        "Return only output matching the supplied JSON schema."
    )
    assert config["workspaces"] == [str(workspace.resolve())]
    assert config["save_dir"] == str((session_dir / "save").resolve())
    assert config["app_data_dir"] == str((session_dir / "app").resolve())
    assert config["response_schema"] == schema
    assert config["model"] == "gemini-test"
    assert config["api_key"] == "test-gemini-key"
    assert config["vertex"] is False
    assert config["tools"] == []
    assert config["mcp_servers"] == []
    assert config["subagents"] == []
    assert config["skills_paths"] == []
    capabilities = config["capabilities"]
    assert capabilities.enable_subagents is False
    assert capabilities.enabled_tools == [
        FakeBuiltinTools.LIST_DIR,
        FakeBuiltinTools.SEARCH_DIR,
        FakeBuiltinTools.FIND_FILE,
        FakeBuiltinTools.VIEW_FILE,
        FakeBuiltinTools.FINISH,
    ]
    assert FakeBuiltinTools.RUN_COMMAND not in capabilities.enabled_tools
    assert FakeBuiltinTools.CREATE_FILE not in capabilities.enabled_tools
    assert FakeBuiltinTools.EDIT_FILE not in capabilities.enabled_tools
    assert FakeBuiltinTools.SEARCH_WEB not in capabilities.enabled_tools
    assert FakeBuiltinTools.START_SUBAGENT not in capabilities.enabled_tools

    trace = [json.loads(line) for line in result.trace_jsonl.splitlines()]
    assert [record["kind"] for record in trace] == ["step", "provider_usage", "normalized_result"]
    assert trace[0]["step"]["id"] == "new"
    assert all(record.get("step", {}).get("id") != "old" for record in trace)
    assert trace[1]["usage"]["thoughts_token_count"] == 5


def test_resume_is_strict_and_reuses_session_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = make_sdk(
        response=FakeResponse(structured_output={"corrected": True}, usage=FakeUsage()),
        current_steps=[FakeStep("resume-step", "DONE")],
    )
    runtime = make_runtime(monkeypatch, sdk)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_dir = tmp_path / "session"
    thread_id = "abcdef12-1234-1234-1234-123456789012"

    result = asyncio.run(
        runtime.resume_agent(
            thread_id,
            "Correct the anchors.",
            AgentRole.REVISION,
            workspace,
            {"type": "object"},
            session_dir,
        )
    )

    assert result.status == "completed"
    config = FakeAgent.instances[0].config.kwargs
    assert config["conversation_id"] == thread_id
    assert config["session_continuation_mode"] is FakeSessionContinuationMode.RESUME
    assert "CREATE_OR_RESUME" not in str(config["session_continuation_mode"])
    assert config["save_dir"] == str((session_dir / "save").resolve())
    assert config["app_data_dir"] == str((session_dir / "app").resolve())


def test_missing_structured_output_and_sdk_errors_are_failed_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_output_sdk = make_sdk(
        response=FakeResponse(structured_output=None, usage=FakeUsage()),
        current_steps=[FakeStep("finish", "DONE")],
    )
    missing_output = asyncio.run(
        make_runtime(monkeypatch, missing_output_sdk).run_agent(
            "Review.",
            AgentRole.CONSISTENCY,
            tmp_path,
            {"type": "object"},
            tmp_path / "state-a",
        )
    )
    assert missing_output.status == "failed"
    assert missing_output.final_response is None
    assert missing_output.error == "Antigravity turn did not return structured output."

    failed_sdk = make_sdk(
        response=FakeResponse(error=RuntimeError("resume state missing")),
        current_steps=[FakeStep("error", "ERROR", error="resume state missing")],
        conversation_id="abcdef12-1234-1234-1234-123456789012",
    )
    failed = asyncio.run(
        make_runtime(monkeypatch, failed_sdk).resume_agent(
            "abcdef12-1234-1234-1234-123456789012",
            "Resume.",
            AgentRole.REVISION,
            tmp_path,
            {"type": "object"},
            tmp_path / "state-b",
        )
    )
    assert failed.status == "failed"
    assert failed.error == "resume state missing"
    assert failed.thread_id == "abcdef12-1234-1234-1234-123456789012"
    assert json.loads(failed.trace_jsonl.splitlines()[0])["step"]["id"] == "error"


def test_sdk_cancellation_is_interrupted_and_external_cancellation_is_reraised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk_cancelled_response = FakeResponse(error=FakeAntigravityCancelledError("backend cancelled"))
    sdk_cancelled_sdk = make_sdk(
        response=sdk_cancelled_response,
        current_steps=[FakeStep("cancelled", "CANCELED")],
    )
    interrupted = asyncio.run(
        make_runtime(monkeypatch, sdk_cancelled_sdk).run_agent(
            "Review.",
            AgentRole.COPYEDIT,
            tmp_path,
            {"type": "object"},
            tmp_path / "state-a",
        )
    )
    assert interrupted.status == "interrupted"
    assert interrupted.error == "backend cancelled"
    assert sdk_cancelled_response.cancelled is True

    external_cancelled_response = FakeResponse(error=asyncio.CancelledError())
    external_cancelled_sdk = make_sdk(
        response=external_cancelled_response,
        current_steps=[FakeStep("cancelled", "CANCELED")],
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            make_runtime(monkeypatch, external_cancelled_sdk).run_agent(
                "Review.",
                AgentRole.COPYEDIT,
                tmp_path,
                {"type": "object"},
                tmp_path / "state-b",
            )
        )
    assert external_cancelled_response.cancelled is True


def test_missing_key_and_sdk_version_fail_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = make_sdk(response=FakeResponse(), current_steps=[])
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeUnavailable, match="GEMINI_API_KEY"):
        AntigravityAgentRuntime(
            route="gemini",
            model="gemini-test",
            provider="gemini",
            reasoning="high",
            sdk=sdk,
        )

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def missing_sdk(_name: str) -> Any:
        raise ModuleNotFoundError("google.antigravity")

    monkeypatch.setattr("scriptorium.antigravity_runtime.import_module", missing_sdk)
    with pytest.raises(RuntimeUnavailable, match=rf"google-antigravity=={ANTIGRAVITY_SDK_VERSION}"):
        AntigravityAgentRuntime(
            route="gemini",
            model="gemini-test",
            provider="gemini",
            reasoning="high",
        )

    monkeypatch.setattr("scriptorium.antigravity_runtime.import_module", lambda _name: SimpleNamespace())
    monkeypatch.setattr("scriptorium.antigravity_runtime.package_version", lambda _name: "0.1.9")
    with pytest.raises(
        RuntimeUnavailable,
        match=rf"google-antigravity=={ANTIGRAVITY_SDK_VERSION}.*found 0\.1\.9",
    ):
        AntigravityAgentRuntime(
            route="gemini",
            model="gemini-test",
            provider="gemini",
            reasoning="high",
        )
