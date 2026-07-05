"""服务启动入口。

本文件只负责把各个子系统按依赖顺序装配起来，不承载具体业务逻辑：
配置读取、日志、存储、记忆、画像、工具、流程运行时和外部接口都在这里连接。
"""

from __future__ import annotations

import logging
import time

from .config.config import get_settings
from .flows.dialogue import DialogueService
from .flows.flow_runtime import FlowConfig, FlowRuntime
from .flows.ingest import EventIngestService
from .flows.proactive import L1ProactiveService
from .interfaces import InterfaceManager
from .log.log import configure_logging
from .memory.consolidator import MemoryConsolidator
from .memory.scheduler import MemoryScheduler
from .profile import ProfileUpdater
from .schedule.service import ScheduleTriggerService
from .storage.schedule_store import ScheduleStore
from .storage.storage import GlobalEventStore, MemoryStore


def _flow_configs_from_settings(settings) -> dict[str, FlowConfig]:
    """把配置层的 FlowSettings 转换成运行时 FlowConfig。"""
    configs: dict[str, FlowConfig] = {}
    for level, flow in settings.flows.items():
        parsed = str(level or "").strip().upper()
        configs[parsed] = FlowConfig(
            level=parsed,
            inputs=flow.inputs,
            outputs=flow.outputs,
            wait_for_result=flow.wait_for_result,
        )
    return configs


def main() -> None:
    """启动常驻服务：加载配置、初始化依赖、启动分层流程运行时。"""
    # 配置对象会同时读取通用配置与接口配置，并在构造阶段确保 data/log 目录存在。
    settings = get_settings()
    flow_configs = _flow_configs_from_settings(settings)

    # 日志必须尽早初始化，后续服务启动、接口连接、worker 异常都会依赖它记录上下文。
    logger = logging.getLogger(__name__)
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
    profile_updater = ProfileUpdater(
        memory_store,
        settings,
        max_tokens=settings.profile_max_tokens,
    )

    # 调度器是常驻后台组件：定时触发记忆整理，并在启用画像时顺带刷新 persona/user。
    scheduler = (
        MemoryScheduler(consolidator, profile_updater=profile_updater)
        if settings.scheduler_enabled
        else None
    )
    if scheduler is not None:
        scheduler.start()

    # L0 用户对话服务
    dialogue_service = DialogueService(
        event_store,
        consolidator=consolidator,
        settings=settings,
    )

    # L1 主动触发服务
    proactive_service = L1ProactiveService(event_store, memory_store, settings)

    # L2/L3 背景事件服务
    ingest_service = EventIngestService(event_store, consolidator)

    # 接口管理器
    interface_manager = InterfaceManager.from_settings(settings.interfaces, flow_configs)

    # FlowRuntime 是流程调度核心：每个注意力层一个队列和 worker，接口只需调用 submit。
    runtime = FlowRuntime(
        dialogue_service,
        proactive_service,
        ingest_service,
        settings,
        flow_configs=flow_configs,
        output_dispatcher=interface_manager,
        interface_names=interface_manager.names,
    )
    runtime.start()
    interface_manager.start(runtime.submit)

    # 日程表定时触发器
    schedule_trigger_service = ScheduleTriggerService(schedule_store, runtime.submit)
    schedule_trigger_service.start()

    # 启动日志
    logger.info(
        "Wintermute服务启动 data_dir=%s log_path=%s model=%s interfaces=%s",
        settings.data_dir,
        log_path,
        settings.model,
        ",".join(interface_manager.names) or "(none)",
    )
    logger.info("Wintermute 分层流程运行时已启动")

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



if __name__ == "__main__":
    main()
