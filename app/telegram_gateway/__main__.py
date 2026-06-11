from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from .config import DEFAULT_CONFIG_PATH, load_config
from .gateway import TelegramGateway, build_http_server
from .telegram import TelegramClient
from .wintermute import WintermuteClient


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    config = load_config(args.config)
    telegram = TelegramClient(config.bot_token)
    wintermute = WintermuteClient(config.wintermute_event_url)
    gateway = TelegramGateway(config, telegram, wintermute)

    gateway.start()
    server = build_http_server(gateway)
    telegram.set_webhook(config.webhook_url, config.webhook_secret_token)

    logging.getLogger(__name__).info(
        "Telegram gateway started host=%s port=%s path=%s",
        config.host,
        config.port,
        config.path,
    )
    print(f"Telegram 网关已启动: http://{config.host}:{config.port}{config.path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Telegram gateway stopped")
    finally:
        server.server_close()
        gateway.stop()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wintermute Telegram webhook gateway")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Telegram gateway config path",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    main()
