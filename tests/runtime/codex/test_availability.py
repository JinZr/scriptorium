from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scriptorium.runtime import RuntimeUnavailable
from scriptorium.runtime.codex import CodexAgentRuntime


def test_missing_sdk_raises_clear_runtime_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_sdk(name: str) -> Any:
        assert name == "openai_codex"
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("scriptorium.runtime.codex.import_module", missing_sdk)

    with pytest.raises(RuntimeUnavailable, match=r"openai-codex==0\.144\.4"):
        CodexAgentRuntime(route="primary", model="model", provider="openai", reasoning="high")


def test_wrong_sdk_version_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scriptorium.runtime.codex.import_module",
        lambda name: SimpleNamespace(__version__="0.145.0"),
    )

    with pytest.raises(RuntimeUnavailable, match=r"found 0\.145\.0"):
        CodexAgentRuntime(route="primary", model="model", provider="openai", reasoning="high")
