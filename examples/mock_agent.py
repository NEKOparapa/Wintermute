"""在本地运行完整的智能体与工具调用循环，无需凭据或网络。"""

import asyncio
from collections.abc import AsyncIterator

from pydantic import BaseModel, ConfigDict

from wintermute.agent import (
    AgentContext,
    AgentEvent,
    AgentRunner,
    AssistantMessage,
    ModelEvent,
    TextDelta,
    Tool,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolResultMessage,
)


class AddInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: int
    b: int


async def add(params: AddInput, context: ToolContext) -> ToolResult:
    return ToolResult(str(params.a + params.b))


class DemoModel:
    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        last = context.messages[-1]
        if isinstance(last, ToolResultMessage):
            text = f"Result: {last.content}"
            yield TextDelta(text)
            yield AssistantMessage(text)
        else:
            yield AssistantMessage(
                tool_calls=(ToolCall("demo-add", "add", {"a": 19, "b": 23}),),
                finish_reason="tool_calls",
            )


def display(event: AgentEvent) -> None:
    if event.type == "message_update":
        print(event.delta, end="", flush=True)
    elif event.type == "tool_execution_end" and event.result is not None:
        print(f"[tool] {event.result.content}")


async def main() -> None:
    runner = AgentRunner(DemoModel(), tools=[Tool("add", "Add two integers", AddInput, add)])
    runner.subscribe(display)
    result = await runner.run("What is 19 + 23? Use the add tool.")
    print(f"\nCompleted in {result.turns} model turns.")


if __name__ == "__main__":
    asyncio.run(main())
