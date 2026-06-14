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
from .llm.llm import OpenAICompatibleLLM
from .log.log import configure_logging
from .memory.consolidator import MemoryConsolidator
from .memory.scheduler import MemoryScheduler
from .profile import ProfileStore, ProfileUpdater
from .storage.storage import GlobalEventStore, MemoryStore
from .tools import build_tool_registry

logger = logging.getLogger(__name__)


def main() -> None:
    """启动常驻服务：加载配置、初始化依赖、启动分层流程运行时。"""
    settings = get_settings()
    log_path = configure_logging(settings.log_dir, retention_days=settings.log_retention_days)
    event_store = GlobalEventStore(settings.data_dir)
    memory_store = MemoryStore(settings.data_dir)
    llm = OpenAICompatibleLLM(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
    )
    consolidator = MemoryConsolidator(
        event_store,
        memory_store,
        llm,
    )
    # 画像层：缺失时用 config 模板初始化，并由调度器按 user 每日 / persona 每周自动刷新。
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
            llm,
            max_tokens=settings.profile_max_tokens,
        )
    tool_registry = build_tool_registry(settings)
    service = DialogueService(
        event_store,
        llm,
        consolidator=consolidator,
        tool_registry=tool_registry,
    )
    proactive_service = L1ProactiveService(event_store, memory_store, llm)
    # 背景事件流程：L2/L3 事件只落库并逐条压缩进事件记忆，不唤起主 AI 对话。
    ingest_service = EventIngestService(event_store, consolidator)
    scheduler = (
        MemoryScheduler(consolidator, profile_updater=profile_updater)
        if settings.scheduler_enabled
        else None
    )
    if scheduler is not None:
        scheduler.start()

    flow_configs = _flow_configs_from_settings(settings)
    interface_manager = InterfaceManager.from_settings(settings.interfaces, flow_configs)
    runtime = FlowRuntime(
        service,
        proactive_service,
        ingest_service,
        flow_configs=flow_configs,
        output_dispatcher=interface_manager,
        interface_names=interface_manager.names,
    )
    runtime.start()
    interface_manager.start(runtime.submit)

    logger.info(
        "服务启动 data_dir=%s log_path=%s model=%s tools=%s interfaces=%s",
        settings.data_dir,
        log_path,
        settings.model,
        len(tool_registry) if tool_registry is not None else 0,
        ",".join(interface_manager.names) or "(none)",
    )
    print("Wintermute 分层流程运行时已启动")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("服务退出")
    finally:
        runtime.stop()
        interface_manager.stop()
        if scheduler is not None:
            scheduler.stop()


def _flow_configs_from_settings(settings) -> dict[AttentionLevel, FlowConfig]:
    configs: dict[AttentionLevel, FlowConfig] = {}
    for level, flow in settings.flows.items():
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
