from __future__ import annotations

import pytest

from scriptorium.runtime import RuntimeUnavailable
from scriptorium.runtime.claude_code import ClaudeCodeAgentRuntime

from ._fake_sdk import FakeQuery, _sdk


def test_runtime_requires_exact_sdk_version() -> None:
    with pytest.raises(RuntimeUnavailable, match="requires claude-agent-sdk==0.2.128"):
        ClaudeCodeAgentRuntime(
            route="claude_review",
            model="claude-test",
            provider="anthropic",
            reasoning="high",
            sdk_loader=lambda: _sdk(FakeQuery([]), version="0.2.127"),
        )
