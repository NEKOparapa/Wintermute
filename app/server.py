from __future__ import annotations

import logging

from .config.config import get_settings
from .dialogue import DialogueService
from .http_api import build_http_server
from .ingest import EventIngestService
from .llm.llm import OpenAICompatibleLLM
from .log.log import configure_logging
from .memory.consolidator import MemoryConsolidator
from .memory.scheduler import MemoryScheduler
from .profile import ProfileStore, ProfileUpdater
from .storage.storage import GlobalEventStore, MemoryStore
from .tools import build_tool_registry

logger = logging.getLogger(__name__)


def main() -> None:
    """启动常驻服务：加载配置、初始化依赖、绑定 HTTP 端口并持续运行。"""
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
    # 背景事件流程：L2/L3 事件只落库并逐条压缩进事件记忆，不唤起主 AI 对话。
    ingest_service = EventIngestService(event_store, consolidator)
    scheduler = (
        MemoryScheduler(consolidator, profile_updater=profile_updater)
        if settings.scheduler_enabled
        else None
    )
    if scheduler is not None:
        scheduler.start()

    server = build_http_server(service, ingest_service, settings.host, settings.port)
    logger.info(
        "服务启动 host=%s port=%s data_dir=%s log_path=%s model=%s tools=%s",
        settings.host,
        settings.port,
        settings.data_dir,
        log_path,
        settings.model,
        len(tool_registry) if tool_registry is not None else 0,
    )
    print(f"Wintermute 服务已启动: http://{settings.host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务退出")
    finally:
        if scheduler is not None:
            scheduler.stop()
        server.server_close()


if __name__ == "__main__":
    main()
