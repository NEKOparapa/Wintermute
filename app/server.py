from __future__ import annotations

import logging
from datetime import date, timedelta

from .config.config import Settings
from .dialogue import DialogueService
from .http_api import build_http_server
from .llm.llm import OpenAICompatibleLLM
from .log.log import configure_logging
from .memory.consolidator import Consolidator
from .memory.memory import MemoryStore
from .memory.orchestrator import MemoryOrchestrator
from .memory.tokens import TokenCounter
from .scheduler.scheduler import Scheduler
from .storage.storage import GlobalEventStore

logger = logging.getLogger(__name__)


def main() -> None:
    """启动常驻服务:加载配置、初始化依赖、绑定 HTTP 端口、启动调度器并持续运行。"""
    settings = Settings.load()
    log_path = configure_logging(settings.log_dir, retention_days=settings.log_retention_days)

    # ---------- 依赖装配 ----------
    llm = OpenAICompatibleLLM(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
    )
    token_counter = TokenCounter(model=settings.model)
    events_store = GlobalEventStore(settings.data_dir)
    memory_store = MemoryStore(settings.data_dir)
    consolidator = Consolidator(llm, token_counter)
    orchestrator = MemoryOrchestrator(events_store, memory_store, consolidator)

    service = DialogueService(
        events_store,
        llm,
        memory_store,
        consolidator,
        token_counter,
    )

    # ---------- 调度器:三层定时压缩 ----------
    scheduler = _build_scheduler(orchestrator)

    # ---------- 启动时补救最近错过的 rollup ----------
    try:
        produced = orchestrator.catch_up_recent()
        logger.info(
            "启动补救完成 daily=%d weekly=%d monthly=%d",
            len(produced["daily"]),
            len(produced["weekly"]),
            len(produced["monthly"]),
        )
    except Exception:
        logger.exception("启动补救失败,跳过")

    scheduler.start()

    # ---------- HTTP 服务 ----------
    server = build_http_server(service, orchestrator, settings.host, settings.port)
    logger.info(
        "服务启动 host=%s port=%s data_dir=%s log_path=%s model=%s",
        settings.host,
        settings.port,
        settings.data_dir,
        log_path,
        settings.model,
    )
    print(f"Wintermute 服务已启动: http://{settings.host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务退出")
    finally:
        scheduler.stop()
        server.server_close()


def _build_scheduler(orchestrator: MemoryOrchestrator) -> Scheduler:
    """注册 daily/weekly/monthly 三个定时压缩任务。所有 action 都是幂等的。"""
    scheduler = Scheduler()

    # 每天 03:00 压缩昨天。
    scheduler.add_daily(
        name="daily_rollup",
        hour=3,
        minute=0,
        action=lambda now: orchestrator.rollup_daily(now.date() - timedelta(days=1)),
    )

    # 每周一 03:30 压缩上一个 ISO 周。
    def _run_weekly(now):
        prev = (now - timedelta(days=now.isoweekday())).isocalendar()
        orchestrator.rollup_weekly(prev.year, prev.week)

    scheduler.add_weekly(
        name="weekly_rollup",
        weekday=0,  # 周一
        hour=3,
        minute=30,
        action=_run_weekly,
    )

    # 每月 1 号 04:00 压缩上个月。
    def _run_monthly(now):
        if now.month == 1:
            orchestrator.rollup_monthly(now.year - 1, 12)
        else:
            orchestrator.rollup_monthly(now.year, now.month - 1)

    scheduler.add_monthly(
        name="monthly_rollup",
        day=1,
        hour=4,
        minute=0,
        action=_run_monthly,
    )

    return scheduler


if __name__ == "__main__":
    main()
