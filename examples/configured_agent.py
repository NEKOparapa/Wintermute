"""从 JSON 创建真实模型、主/子 Agent 和事件运行层；显式执行才调用 API。"""

import argparse
import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

from wintermute.agent import AgentRunner
from wintermute.agent.tools import BackgroundSubagentManager
from wintermute.config import ConfigManager
from wintermute.config.factory import (
    configure_app_logging,
    create_background_manager,
    create_runner,
    create_scheduler,
    create_store,
    open_models,
)
from wintermute.events import (
    BackgroundEventPublisher,
    EventGateway,
    OutputRecord,
    RuntimeEvent,
    Terminal,
)


def display(event: RuntimeEvent) -> None:
    if event.event.type == "message_update" and event.event.delta:
        print(event.event.delta, end="", flush=True)


def acknowledge(record: OutputRecord) -> None:
    # 流式增量仅供显示；完整输出仍持久化，并以成功返回确认终端收取。
    print(f"\n[{record.layer}] 已完成：{record.job_id}")


async def main(config_path: Path, prompt: str, conversation_id: str) -> None:
    manager = ConfigManager(config_path)
    config = await asyncio.to_thread(manager.load)
    with (
        configure_app_logging(config, base_dir=manager.base_dir),
        create_store(config.events, base_dir=manager.base_dir) as store,
    ):
        gateway = EventGateway(store)
        gateway.register_terminal(
            Terminal(terminal_id="cli", user_id="local", allowed_layers=("conversation",))
        )
        gateway.register_terminal(
            Terminal(terminal_id="workers", user_id="local", allowed_layers=("notification",))
        )
        gateway.register_conversation(conversation_id, "local")
        chat = gateway.conversation.connect("cli", conversation_id)
        async with open_models(config) as models:
            backgrounds: dict[str, BackgroundSubagentManager] = {}
            closing = False

            def make_runner(runner_conversation_id: str) -> AgentRunner:
                if closing:
                    raise RuntimeError("示例正在关闭，不再创建新会话运行器")
                background = backgrounds.get(runner_conversation_id)
                if background is None and config.subagents:
                    # 调度器会恢复数据库中其他会话；子任务通知必须回到其所属会话。
                    notifications = gateway.notification.connect(
                        "workers", runner_conversation_id, reply_terminal_id="cli"
                    )
                    background = create_background_manager(
                        config,
                        models,
                        base_dir=manager.base_dir,
                        notification_publisher=BackgroundEventPublisher(notifications),
                    )
                    backgrounds[runner_conversation_id] = background
                    # 工厂是同步接口；管理器在首次使用时启动，这里登记异步清理。
                    stack.push_async_callback(background.aclose)
                return create_runner(
                    config,
                    models.main,
                    base_dir=manager.base_dir,
                    additional_tools=background.tools if background is not None else (),
                )

            def background_snapshot() -> dict[str, tuple[str, ...]]:
                return {
                    name: tuple(task.task_id for task in background.list_tasks())
                    for name, background in backgrounds.items()
                }

            scheduler = create_scheduler(
                config.events,
                gateway,
                make_runner,
                slice_model=models.main,
                image_model=models.image_model,
                audio_summary_model=models.audio_summary,
                timeline_summary_model=models.timeline_summary,
                base_dir=manager.base_dir,
            )
            scheduler.subscribe(display)
            scheduler.output_router.connect("cli", acknowledge)
            # 退出顺序：后台任务、调度器、模型客户端、事件库和日志。
            async with scheduler, AsyncExitStack() as stack:
                try:
                    receipt = await chat.send(prompt)
                    assert receipt.job_id is not None
                    job = await scheduler.wait(receipt.job_id)
                    if job.status != "completed":
                        raise RuntimeError(f"Agent 运行失败：{job.status}，{job.error}")
                    # 通知轮次可能创建新管理器或后续子任务；检查所有会话直到稳定空闲。
                    while True:
                        await scheduler.wait_until_idle()
                        before = background_snapshot()
                        await asyncio.gather(
                            *(background.wait_until_idle() for background in backgrounds.values())
                        )
                        await scheduler.wait_until_idle()
                        if before == background_snapshot() and all(
                            task.status in {"completed", "error", "cancelled"}
                            and task.notification_status in {"published", "failed", "suppressed"}
                            for background in backgrounds.values()
                            for task in background.list_tasks()
                        ):
                            break
                finally:
                    closing = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="读取 README.md，简要介绍这个项目。")
    parser.add_argument("--config", type=Path, default=Path("config/settings.json"))
    parser.add_argument("--conversation", default="configured-demo", help="复用此会话的持久历史")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.config, arguments.prompt, arguments.conversation))
