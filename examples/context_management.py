"""离线演示主 Agent 与子 Agent 使用独立预算、摘要器和上下文状态。"""

import asyncio
import json
from collections.abc import AsyncIterator

from pydantic import BaseModel, ConfigDict

from wintermute.agent import (
    AgentContext,
    AgentEvent,
    AgentRunner,
    AssistantMessage,
    ModelEvent,
    Tool,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from wintermute.agent.context import (
    ContextBudget,
    MainContextManager,
    SubagentContextManager,
    SummaryRequest,
)
from wintermute.agent.tools import SubagentConfig, SubagentEvent, create_subagent_tool


class DemoSummarizer:
    """用确定性摘要替代摘要模型，便于观察隔离；生产环境可换成 ModelSummarizer。"""

    async def summarize(self, request: SummaryRequest) -> str:
        texts = [request.previous_summary or ""]
        for message in request.messages:
            if isinstance(message, ToolResultMessage):
                texts.append(message.content)
            else:
                texts.append(message.text)
                if isinstance(message, AssistantMessage):
                    texts.extend(json.dumps(call.arguments) for call in message.tool_calls)
        transcript = "\n".join(texts)
        tags = [tag for tag in ("child-A", "child-B") if tag in transcript]
        return f"{request.role} 摘要：保留 {','.join(tags) or '主任务'}；早期步骤已处理。"


class SampleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tag: str
    step: int


async def sample(params: SampleInput, context: ToolContext) -> ToolResult:
    """产生较长的工具结果，让子 Agent 在一个任务中多次触发预算检查。"""
    await asyncio.sleep(0)
    header = json.dumps({"tag": params.tag, "step": params.step})
    return ToolResult(f"{header}\n" + "observed detail; " * 45)


class WorkerModel:
    """只从最新工具结果读取进度，早期工具记录允许压缩为摘要。"""

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        latest = context.messages[-1]
        if isinstance(latest, UserMessage):
            tag, step = latest.text, 1
        else:
            assert isinstance(latest, ToolResultMessage) and not latest.is_error
            progress = json.loads(latest.content.splitlines()[0])
            tag, step = progress["tag"], progress["step"] + 1
        if step <= 5:
            yield AssistantMessage(
                tool_calls=(ToolCall(f"{tag}-{step}", "sample", {"tag": tag, "step": step}),),
                finish_reason="tool_calls",
            )
        else:
            yield AssistantMessage(f"{tag} 已完成 5 个步骤。")


class MainModel:
    """先记录多轮输入，再真实调用前台 subagent 工具并汇总结果。"""

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        latest = context.messages[-1]
        if isinstance(latest, UserMessage) and latest.text == "委派并行子任务":
            yield AssistantMessage(
                tool_calls=(
                    ToolCall(
                        "delegate",
                        "subagent",
                        {
                            "tasks": [
                                {"agent": "worker", "task": tag} for tag in ("child-A", "child-B")
                            ]
                        },
                    ),
                ),
                finish_reason="tool_calls",
            )
        elif isinstance(latest, ToolResultMessage):
            assert not latest.is_error, latest.content
            items = json.loads(latest.content)["results"]
            assert all(item["status"] == "completed" for item in items)
            yield AssistantMessage("；".join(item["output"] for item in items))
        else:
            yield AssistantMessage("已记录本轮资料。")


def display_context(label: str, event: AgentEvent) -> None:
    if event.type == "context_prepared":
        print(
            f"[{label}] 输入估算={event.context_tokens}/{event.context_limit}，"
            f"本轮压缩={event.compacted}"
        )


async def main() -> None:
    children: list[SubagentContextManager] = []
    child_history_sizes: dict[str, int] = {}

    def child_context() -> SubagentContextManager:
        # 工厂在每个实际子任务启动时调用，不能返回同一个有状态实例。
        manager = SubagentContextManager(
            ContextBudget(
                context_window=3200,
                reserve_tokens=500,
                keep_recent_tokens=800,
                max_summary_tokens=250,
            ),
            DemoSummarizer(),
        )
        children.append(manager)
        return manager

    def display_child(update: SubagentEvent) -> None:
        display_context(update.task, update.event)
        if update.event.type == "message_end":
            child_history_sizes[update.task] = child_history_sizes.get(update.task, 0) + 1

    subagent = create_subagent_tool(
        [
            SubagentConfig(
                "worker",
                WorkerModel(),
                system_prompt="完成指定任务的五个步骤。",
                tools=[
                    Tool("sample", "Collect the next step's observations.", SampleInput, sample)
                ],
                context_manager_factory=child_context,
            )
        ],
        max_concurrency=2,
        on_event=display_child,
    )
    main_context = MainContextManager(
        ContextBudget(
            context_window=6500,
            reserve_tokens=1000,
            keep_recent_tokens=1200,
            max_summary_tokens=300,
        ),
        DemoSummarizer(),
    )
    runner = AgentRunner(MainModel(), tools=[subagent], context_manager=main_context)
    runner.subscribe(lambda event: display_context("主 Agent", event))

    for index in range(10):
        await runner.run(f"第 {index + 1} 轮资料：" + "background note; " * 50)
    result = await runner.run("委派并行子任务")
    assert runner.context_state is not None and runner.context_state.compactions > 0
    assert len(children) == 2 and children[0] is not children[1]
    assert all(manager.state.compactions > 0 for manager in children)
    child_summaries = [manager.state.summary for manager in children]
    assert any("child-A" in text and "child-B" not in text for text in child_summaries)
    assert any("child-B" in text and "child-A" not in text for text in child_summaries)
    print(f"\n{result.response.text}")
    print(
        f"主 Agent 保留完整历史 {len(runner.state.messages)} 条，"
        f"摘要压缩 {runner.context_state.compactions} 次。"
    )
    for index, manager in enumerate(children, start=1):
        tag = "child-A" if "child-A" in manager.state.summary else "child-B"
        print(
            f"子 Agent {index}：原始消息 {child_history_sizes[tag]} 条，"
            f"压缩 {manager.state.compactions} 次，{manager.state.summary}"
        )


if __name__ == "__main__":
    asyncio.run(main())
