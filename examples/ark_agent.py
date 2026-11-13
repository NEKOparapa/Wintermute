"""通过方舟接口运行流式 Responses 工具调用闭环。

先执行 ``uv sync --extra openai`` 安装依赖，设置 ARK_API_KEY、ARK_BASE_URL 和
ARK_MODEL，再运行 ``uv run --extra openai python examples/ark_agent.py``。
"""

import asyncio
import os

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict

from wintermute.agent import AgentEvent, AgentRunner, Tool, ToolContext, ToolResult
from wintermute.agent.providers import OpenAIModel


class AddInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    a: int
    b: int


async def add(arguments: AddInput, context: ToolContext) -> ToolResult:
    return ToolResult(str(arguments.a + arguments.b))


def display(event: AgentEvent) -> None:
    if event.type == "message_update" and event.delta:
        print(event.delta, end="", flush=True)
    elif event.type == "tool_execution_end" and event.tool_call and event.result:
        print(f"\n[tool] {event.tool_call.name}: {event.result.content}")


async def main() -> None:
    required = ("ARK_API_KEY",)
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit("Set these environment variables first: " + ", ".join(missing))
    async with AsyncOpenAI(
        api_key=os.environ["ARK_API_KEY"],
        base_url=os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
        timeout=60.0,
        max_retries=0,
    ) as client:
        model = OpenAIModel(
            client,
            os.environ.get("ARK_MODEL", "doubao-seed-2-0-lite-260428"),
            extra_body={"thinking": {"type": "disabled"}},
        )
        runner = AgentRunner(
            model,
            system_prompt="请先调用 add 工具计算用户要求的加法，再根据工具返回值回答。",
            tools=[Tool("add", "计算两个整数的和", AddInput, add)],
            max_turns=4,
            model_timeout=90,
            tool_timeout=10,
        )
        runner.subscribe(display)
        result = await runner.run("请调用 add 工具计算 17 + 25，然后告诉我结果。")
        print(f"\n完成，共 {result.turns} 轮模型调用。最终结果：{result.response.text}")


if __name__ == "__main__":
    asyncio.run(main())
