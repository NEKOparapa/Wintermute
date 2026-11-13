"""通过模拟模型演示子 Agent 并行执行；文件读取使用真实工具，无需 API Key。"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory

from wintermute.agent import (
    AgentContext,
    AgentRunner,
    AssistantMessage,
    ModelEvent,
    TextDelta,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from wintermute.agent.tools import (
    SubagentConfig,
    SubagentEvent,
    create_read_tool,
    create_subagent_tool,
)


class ReaderModel:
    """读取临时目录中的说明文件，并将摘要交给父 Agent。"""

    def __init__(self, barrier: asyncio.Barrier) -> None:
        self.barrier = barrier

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        assert context.system_prompt == "你是资料员，只读取资料并返回事实摘要。"
        assert [tool.name for tool in context.tools] == ["read"]
        if len(context.messages) == 1:
            assert isinstance(context.messages[0], UserMessage)
            yield TextDelta("开始读取资料。")
            # 两个子任务都进入模型后才继续，演示实际的并发执行。
            await self.barrier.wait()
            yield AssistantMessage(
                tool_calls=(ToolCall("read-note", "read", {"path": "note.txt"}),),
                finish_reason="tool_calls",
            )
        else:
            result = context.messages[-1]
            assert isinstance(result, ToolResultMessage) and not result.is_error
            answer = f"文件内容：{result.content.strip()}"
            yield TextDelta(answer)
            yield AssistantMessage(answer)


class PlannerModel:
    """只接收自己的任务，使用独立提示词，不开放本地工具。"""

    def __init__(self, barrier: asyncio.Barrier) -> None:
        self.barrier = barrier

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        assert context.system_prompt == "你是测试规划员，给出简短的检查要点。"
        assert context.tools == ()
        assert len(context.messages) == 1
        assert isinstance(context.messages[0], UserMessage)
        yield TextDelta("开始制定检查要点。")
        await self.barrier.wait()
        answer = "检查上下文隔离、并发上限、结果顺序，以及取消后的资源清理。"
        yield TextDelta(answer)
        yield AssistantMessage(answer)


class CoordinatorModel:
    """发起批量委派，收到两个子任务结果后生成最终回答。"""

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        if isinstance(context.messages[-1], UserMessage):
            yield AssistantMessage(
                tool_calls=(
                    ToolCall(
                        "delegate-tasks",
                        "subagent",
                        {
                            "tasks": [
                                {"agent": "reader", "task": "读取 note.txt 并概括内容。"},
                                {"agent": "planner", "task": "列出子 Agent 并发执行的检查要点。"},
                            ]
                        },
                    ),
                ),
                finish_reason="tool_calls",
            )
        else:
            result = context.messages[-1]
            assert isinstance(result, ToolResultMessage) and not result.is_error
            items = json.loads(result.content)["results"]
            assert [item["agent"] for item in items] == ["reader", "planner"]
            assert all(item["status"] == "completed" for item in items)
            answer = "\n".join(f"{item['agent']}：{item['output']}" for item in items)
            yield AssistantMessage(f"已汇总两个独立子任务：\n{answer}")


def display_child(update: SubagentEvent) -> None:
    """按父工具调用和任务索引区分并行子任务的流式事件。"""
    label = f"{update.call_id}/{update.task_index}:{update.agent}"
    event = update.event
    if event.type == "message_update" and event.delta:
        print(f"[{label}] {event.delta}")
    elif event.type == "tool_execution_end" and event.tool_call:
        print(f"[{label}] 工具 {event.tool_call.name} 执行结束")
    elif event.type == "agent_end":
        print(f"[{label}] {event.status}")


async def main() -> None:
    with TemporaryDirectory(prefix="wintermute-subagents-") as workspace:
        await asyncio.to_thread(
            Path(workspace, "note.txt").write_text,
            "Wintermute 使用独立 AgentRunner 执行子任务，并向父 Agent 返回最终结果。\n",
            encoding="utf-8",
        )
        barrier = asyncio.Barrier(2)
        subagent = create_subagent_tool(
            [
                SubagentConfig(
                    "reader",
                    ReaderModel(barrier),
                    description="读取资料并返回事实摘要",
                    system_prompt="你是资料员，只读取资料并返回事实摘要。",
                    tools=[create_read_tool(workspace)],
                    timeout=10,
                ),
                SubagentConfig(
                    "planner",
                    PlannerModel(barrier),
                    description="提供测试检查要点",
                    system_prompt="你是测试规划员，给出简短的检查要点。",
                    timeout=10,
                ),
            ],
            max_concurrency=2,
            on_event=display_child,
        )
        runner = AgentRunner(CoordinatorModel(), tools=[subagent], tool_timeout=30)
        result = await runner.run("同时阅读说明并提出并发测试要点，然后综合两个结果。")
        print(f"\n{result.response.text}\n父 Agent 共执行 {result.turns} 个模型轮次。")


if __name__ == "__main__":
    asyncio.run(main())
