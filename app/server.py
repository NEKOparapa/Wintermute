from __future__ import annotations

import logging

from .config.config import Settings
from .dialogue import DialogueService
from .http_api import build_http_server
from .llm.llm import OpenAICompatibleLLM
from .log.log import configure_logging
from .storage.storage import GlobalEventStore

logger = logging.getLogger(__name__)


def main() -> None:
    """启动常驻服务：加载配置、初始化依赖、绑定 HTTP 端口并持续运行。"""
    settings = Settings.load()
    log_path = configure_logging(settings.log_dir, retention_days=settings.log_retention_days)
    service = DialogueService(
        GlobalEventStore(settings.data_dir),
        OpenAICompatibleLLM(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
        ),
    )

    server = build_http_server(service, settings.host, settings.port)
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
        server.server_close()


if __name__ == "__main__":
    main()
