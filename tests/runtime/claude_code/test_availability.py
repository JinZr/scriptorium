from __future__ import annotations

import pytest

from scriptorium.runtime import RuntimeUnavailable
from scriptorium.runtime.claude_code import ClaudeCodeAgentRuntime

from ._fake_sdk import FakeQuery, _sdk


def test_runtime_requires_exact_distribution_version(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = _sdk(FakeQuery([]))
    monkeypatch.setattr("scriptorium.runtime.claude_code.import_module", lambda _name: sdk)
    monkeypatch.setattr("scriptorium.runtime.claude_code.package_version", lambda _name: "0.2.127")

    with pytest.raises(RuntimeUnavailable, match="requires claude-agent-sdk==0.2.128"):
        ClaudeCodeAgentRuntime(
            route="claude_review",
            model="claude-test",
            provider="anthropic",
            reasoning="high",
        )


def test_runtime_uses_distribution_version_without_module_version(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = _sdk(FakeQuery([]))
    del sdk.__version__
    monkeypatch.setattr("scriptorium.runtime.claude_code.import_module", lambda _name: sdk)
    monkeypatch.setattr("scriptorium.runtime.claude_code.package_version", lambda _name: "0.2.128")

    runtime = ClaudeCodeAgentRuntime(
        route="claude_review",
        model="claude-test",
        provider="anthropic",
        reasoning="high",
    )

    assert runtime._runtime_version == "0.2.128"
