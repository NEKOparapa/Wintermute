"""单轮系统提示的历史隔离与预算边界。"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from wintermute.agent import AgentContext, AgentRunner, AssistantMessage, RunControl, ToolCall
from wintermute.agent.context import (
    ContextBudget,
    ContextLimitError,
    MainContextManager,
    SummaryRequest,
)


class RecordingModel:
    def __init__(self, *replies: AssistantMessage) -> None:
        self.replies = iter(replies)
        self.requests: list[AgentContext] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[AssistantMessage]:
        self.requests.append(context)
        yield next(self.replies)


def control() -> RunControl:
    return RunControl(
        wake=asyncio.Event(),
        has_inputs=lambda: False,
        adopt=lambda _: ((), ()),
        finish=lambda transcript, response: True,
        declare_tools=lambda transcript, response: None,
        start_tool=lambda call: True,
        end_tool=lambda result: None,
    )


async def test_tool_continuation_uses_one_profile_and_next_run_replaces_it() -> None:
    model = RecordingModel(
        AssistantMessage(tool_calls=(ToolCall("lookup", "lookup", {}),)),
        AssistantMessage("完成"),
        AssistantMessage("完成"),
        AssistantMessage("完成"),
    )
    runner = AgentRunner(model, system_prompt="固定规则")
    await runner.run_controlled(
        "查询议程", control=control(), system_prompt_suffix="周会材料发手机"
    )
    await runner.run_controlled(
        "再查一次", control=control(), system_prompt_suffix="周会材料发电脑"
    )
    await runner.run("独立运行")

    assert [ctx.system_prompt for ctx in model.requests] == [
        "固定规则\n\n周会材料发手机",
        "固定规则\n\n周会材料发手机",
        "固定规则\n\n周会材料发电脑",
        "固定规则",
    ]
    assert all("周会材料" not in repr(ctx.messages) for ctx in model.requests)
    assert "周会材料" not in repr(runner.state.messages)


async def test_profile_counts_toward_budget_and_does_not_leak_after_failed_run() -> None:
    class NoHistory:
        async def summarize(self, request: SummaryRequest) -> str:
            raise AssertionError("No prior exchanges to summarize")

    model = RecordingModel(AssistantMessage("完成"))
    manager = MainContextManager(ContextBudget(1000, 100, 100, 100), NoHistory())
    runner = AgentRunner(model, context_manager=manager)
    with pytest.raises(ContextLimitError):
        await runner.run_controlled(
            "核对", control=control(), system_prompt_suffix="超长档案" * 1000
        )
    assert model.requests == []
    await runner.continue_run()
    assert model.requests[0].system_prompt == ""
    assert "超长档案" not in repr(runner.state.messages)
