"""通过方舟 Responses 和本地 read/write/edit/exec 工具执行任务。"""

import argparse
import asyncio
import os
from pathlib import Path

from openai import AsyncOpenAI

from wintermute.agent import AgentEvent, AgentRunner
from wintermute.agent.providers import OpenAIModel
from wintermute.agent.tools import create_builtin_tools


def display(event: AgentEvent) -> None:
    if event.type == "message_update" and event.delta:
        print(event.delta, end="", flush=True)
    elif event.type == "tool_execution_end" and event.tool_call and event.result:
        status = "error" if event.result.is_error else "ok"
        print(f"\n[{event.tool_call.name}: {status}]\n{event.result.content}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="Task for the agent to perform")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Existing working directory")
    parser.add_argument("--shell", help="Shell executable, otherwise detected for the platform")
    args = parser.parse_args()
    if not os.environ.get("ARK_API_KEY"):
        parser.error("Set ARK_API_KEY before running this example")
    workspace = args.cwd.expanduser().resolve(strict=True)
    tools = create_builtin_tools(workspace, shell=args.shell)
    async with AsyncOpenAI(
        api_key=os.environ["ARK_API_KEY"],
        base_url=os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
        timeout=60,
        max_retries=0,
    ) as client:
        model = OpenAIModel(
            client,
            os.environ.get("ARK_MODEL", "doubao-seed-2-0-lite-260428"),
            extra_body={"thinking": {"type": "disabled"}},
        )
        runner = AgentRunner(
            model,
            tools=tools,
            system_prompt=(
                f"You are Wintermute. Your working directory is {workspace}. "
                "Use read to inspect files, write for new files or complete rewrites, "
                "edit for unique targeted replacements, and exec for foreground commands. "
                "Follow the shell syntax stated in the exec description. "
                "Verify your changes and report the result."
            ),
            max_turns=20,
            model_timeout=90,
        )
        runner.subscribe(display)
        result = await runner.run(args.prompt)
        print(f"\nCompleted in {result.turns} model turns.")


if __name__ == "__main__":
    asyncio.run(main())
