import asyncio

import pytest

from scriptorium.runtime import AgentCancelled, AgentResult, AgentUsage


def test_agent_cancellation_preserves_result_across_task_boundary() -> None:
    result = AgentResult(
        thread_id="thread-1",
        status="interrupted",
        final_response=None,
        usage=AgentUsage(),
        trace_jsonl="",
        runtime_name="fake",
        runtime_version="1",
        model="model",
        model_provider="provider",
        duration_ms=None,
        error="cancelled",
    )

    async def scenario() -> AgentCancelled:
        async def invoke() -> None:
            raise AgentCancelled(result)

        task = asyncio.create_task(invoke())
        with pytest.raises(AgentCancelled) as caught:
            await task
        return caught.value

    assert asyncio.run(scenario()).result is result
