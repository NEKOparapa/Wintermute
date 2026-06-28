"""服务启动入口。

本文件只负责把各个子系统按依赖顺序装配起来，不承载具体业务逻辑：
配置读取、日志、存储、记忆、画像、工具、流程运行时和外部接口都在这里连接。
"""

from __future__ import annotations

import logging
import time

from .attention.attention import AttentionLevel, parse_level
from .config.config import get_settings
from .flows.dialogue import DialogueService
from .flows.flow_runtime import FlowConfig, FlowRuntime
from .flows.ingest import EventIngestService
from .flows.proactive import L1ProactiveService
from .interfaces import InterfaceManager
from .log.log import configure_logging
from .memory.consolidator import MemoryConsolidator
from .memory.scheduler import MemoryScheduler
from .profile import ProfileStore, ProfileUpdater
from .schedule.service import ScheduleTriggerService
from .storage.schedule_store import ScheduleStore
from .storage.storage import GlobalEventStore, MemoryStore

logger = logging.getLogger(__name__)


def main() -> None:
    """启动常驻服务：加载配置、初始化依赖、启动分层流程运行时。"""
    # 配置对象会同时读取通用配置与接口配置，并在构造阶段确保 data/log 目录存在。
    settings = get_settings()

    # 日志必须尽早初始化，后续服务启动、接口连接、worker 异常都会依赖它记录上下文。
    log_path = configure_logging(settings.log_dir, retention_days=settings.log_retention_days)

    # 全局事件流和分层记忆共用 data_dir，但分别落在不同子目录，职责保持分离。
    event_store = GlobalEventStore(settings.data_dir)
    memory_store = MemoryStore(settings.data_dir)
    schedule_store = ScheduleStore(settings.data_dir)

    # 记忆压缩器
    consolidator = MemoryConsolidator(
        event_store,
        memory_store,
        settings,
    )
    # 设定更新器
    profile_updater = None
    if settings.profile_enabled:
        profile_store = ProfileStore(
            settings.data_dir,
            soul_path=settings.soul_path,
            persona_template_path=settings.persona_template_path,
            user_template_path=settings.user_template_path,
        )
        profile_store.ensure_seeded()
        profile_updater = ProfileUpdater(
            memory_store,
            profile_store,
            settings,
            max_tokens=settings.profile_max_tokens,
        )

    # L0 用户对话服务
    service = DialogueService(
        event_store,
        consolidator=consolidator,
        settings=settings,
    )

    # L1 主动流程使用独立服务
    proactive_service = L1ProactiveService(event_store, memory_store, settings)

    # L2/L3 背景事件服务
    ingest_service = EventIngestService(event_store, consolidator)

    # 调度器是常驻后台组件：定时触发记忆整理，并在启用画像时顺带刷新 persona/user。
    scheduler = (
        MemoryScheduler(consolidator, profile_updater=profile_updater)
        if settings.scheduler_enabled
        else None
    )
    if scheduler is not None:
        scheduler.start()

    # 将配置文件里的字符串层级转换为运行时使用的 AttentionLevel 枚举。
    flow_configs = _flow_configs_from_settings(settings)

    # InterfaceManager 同时负责外部输入监听和输出分发；未启用接口时 names 为空。
    interface_manager = InterfaceManager.from_settings(settings.interfaces, flow_configs)

    # FlowRuntime 是流程调度核心：每个注意力层一个队列和 worker，接口只需调用 submit。
    runtime = FlowRuntime(
        service,
        proactive_service,
        ingest_service,
        settings,
        flow_configs=flow_configs,
        output_dispatcher=interface_manager,
        interface_names=interface_manager.names,
    )
    runtime.start()

    # 日程触发服务使用现有 L1 流程入口，到点后提交 schedule_trigger 事件。
    schedule_trigger_service = ScheduleTriggerService(schedule_store, runtime.submit)
    schedule_trigger_service.start()

    # 接口在 runtime 启动后再开始监听，避免外部消息进入时 worker 尚未就绪。
    interface_manager.start(runtime.submit)

    logger.info(
        "服务启动 data_dir=%s log_path=%s model=%s interfaces=%s",
        settings.data_dir,
        log_path,
        settings.model,
        ",".join(interface_manager.names) or "(none)",
    )
    print("Wintermute 分层流程运行时已启动")

    # 主线程保活
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("服务退出")
    finally:
        schedule_trigger_service.stop()
        runtime.stop()
        interface_manager.stop()
        if scheduler is not None:
            scheduler.stop()


def _flow_configs_from_settings(settings) -> dict[AttentionLevel, FlowConfig]:
    """把配置层的 FlowSettings 转换成运行时 FlowConfig。"""
    configs: dict[AttentionLevel, FlowConfig] = {}
    for level, flow in settings.flows.items():
        # 配置文件使用 "L0"..."L3" 字符串，运行时统一使用枚举避免分支写错。
        parsed = parse_level(level)
        configs[parsed] = FlowConfig(
            level=parsed,
            inputs=flow.inputs,
            outputs=flow.outputs,
            wait_for_result=flow.wait_for_result,
        )
    return configs


if __name__ == "__main__":
    main()
