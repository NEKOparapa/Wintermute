"""离线演示 JSON 配置、配置驱动的事件运行和结构化日志，无需 API Key。"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory

from wintermute.agent import AgentContext, AgentRunner, AssistantMessage, ModelEvent, UserMessage
from wintermute.config import (
    AgentSettings,
    AppConfig,
    ConfigManager,
    EventSettings,
    LoggingSettings,
)
from wintermute.config.factory import (
    configure_app_logging,
    create_runner,
    create_scheduler,
    create_store,
)
from wintermute.events import EventGateway, OutputRecord, Terminal
from wintermute.logging import get_logger, log_context


class DemoModel:
    """使用实际组装的会话历史验证多轮输入，不连接模型服务。"""

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
        prompt = context.messages[-1]
        assert isinstance(prompt, UserMessage)
        # 带来源的活动也使用 UserMessage，按已有回答数计算交流轮次。
        previous_replies = sum(
            isinstance(message, AssistantMessage) for message in context.messages
        )
        body, _, footer = prompt.text.rpartition("\n\n---\n")
        assert footer.startswith("来源：用户原话") and "\n时间：" in footer
        yield AssistantMessage(f"已收到第 {previous_replies + 1} 条输入：{body}")


async def main() -> None:
    with TemporaryDirectory(prefix="wintermute-config-") as directory:
        manager = ConfigManager(Path(directory, "config", "settings.json"))
        initial = AppConfig(
            agent=AgentSettings(system_prompt="你是配置演示助手。"),
            events=EventSettings(database_path="../data/events.sqlite3"),
            logging=LoggingSettings(console=False, format="json", file="../logs/demo.log"),
        )
        await asyncio.to_thread(manager.save, initial)
        await asyncio.to_thread(manager.load)
        await asyncio.to_thread(manager.update, {"agent": {"max_turns": 6}})
        config = await asyncio.to_thread(manager.reload)
        assert config.agent.max_turns == 6
        outputs: list[OutputRecord] = []
        logger = get_logger("configuration_demo")

        with (
            configure_app_logging(config, base_dir=manager.base_dir),
            create_store(config.events, base_dir=manager.base_dir) as store,
            log_context(conversation_id="demo", terminal_id="desktop"),
        ):
            gateway = EventGateway(store)
            gateway.register_terminal(
                Terminal(terminal_id="desktop", user_id="user", allowed_layers=("conversation",))
            )
            gateway.register_conversation("demo", "user")
            chat = gateway.conversation.connect("desktop", "demo")

            def make_runner(_conversation_id: str) -> AgentRunner:
                return create_runner(config, DemoModel(), base_dir=manager.base_dir)

            scheduler = create_scheduler(config.events, gateway, make_runner)
            scheduler.output_router.connect("desktop", outputs.append)
            async with asyncio.timeout(10), scheduler:
                for prompt in ("你好，配置助手。", "请继续这段交流。"):
                    receipt = await chat.send(prompt)
                    assert receipt.job_id is not None
                    job = await scheduler.wait(receipt.job_id)
                    assert job.status == "completed", job.error
                await scheduler.wait_until_idle()
            assert len(outputs) == 2
            assert outputs[-1].text == "已收到第 2 条输入：请继续这段交流。"
            logger.info("配置演示完成", extra={"completed_jobs": len(outputs)})

        assert config.logging.file is not None
        log_path = manager.resolve_path(config.logging.file)
        content = await asyncio.to_thread(log_path.read_text, encoding="utf-8")
        records = [json.loads(line) for line in content.splitlines()]
        completion = next(record for record in records if record["message"] == "配置演示完成")
        assert completion["context"]["conversation_id"] == "demo"
        assert completion["extra"]["completed_jobs"] == 2
        print("JSON 保存、加载、更新和重载正常；配置驱动的两轮会话已完成。")
        print("已校验相对路径、SQLite 任务和带关联字段的 JSON 日志。")
        print("演示文件位于临时目录，退出后自动清理。")


if __name__ == "__main__":
    asyncio.run(main())
