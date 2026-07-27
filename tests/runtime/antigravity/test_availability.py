from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scriptorium.runtime import RuntimeUnavailable
from scriptorium.runtime.antigravity import ANTIGRAVITY_SDK_VERSION, AntigravityAgentRuntime

from ._fake_sdk import FakeResponse, make_sdk


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

    monkeypatch.setattr("scriptorium.runtime.antigravity.import_module", missing_sdk)
    with pytest.raises(RuntimeUnavailable, match=rf"google-antigravity=={ANTIGRAVITY_SDK_VERSION}"):
        AntigravityAgentRuntime(
            route="gemini",
            model="gemini-test",
            provider="gemini",
            reasoning="high",
        )

    monkeypatch.setattr("scriptorium.runtime.antigravity.import_module", lambda _name: SimpleNamespace())
    monkeypatch.setattr("scriptorium.runtime.antigravity.package_version", lambda _name: "0.1.9")
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
