from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("config/telegram.json")


@dataclass(frozen=True)
class TelegramGatewayConfig:
    """Telegram webhook gateway runtime settings."""

    bot_token: str
    webhook_url: str
    webhook_secret_token: str
    host: str
    port: int
    path: str
    wintermute_event_url: str
    allowed_chat_ids: frozenset[int]
    download_dir: Path
    delete_downloads_after_request: bool

    @classmethod
    def load(cls, config_path: Path | str = DEFAULT_CONFIG_PATH) -> "TelegramGatewayConfig":
        path = Path(config_path)
        with path.open("r", encoding="utf-8") as file:
            raw = json.load(file)
        if not isinstance(raw, dict):
            raise ValueError(f"Telegram 配置文件必须是 JSON 对象: {path}")

        webhook_path = _required_str(raw, "path")
        if not webhook_path.startswith("/"):
            raise ValueError("Telegram 配置 path 必须以 / 开头。")

        allowed_chat_ids = _allowed_chat_ids(raw.get("allowed_chat_ids"))
        if not allowed_chat_ids:
            raise ValueError("Telegram 配置 allowed_chat_ids 不能为空。")

        return cls(
            bot_token=_required_str(raw, "bot_token"),
            webhook_url=_required_str(raw, "webhook_url"),
            webhook_secret_token=_required_str(raw, "webhook_secret_token"),
            host=_required_str(raw, "host"),
            port=_as_int(raw.get("port"), "port"),
            path=webhook_path,
            wintermute_event_url=_required_str(raw, "wintermute_event_url"),
            allowed_chat_ids=frozenset(allowed_chat_ids),
            download_dir=Path(_required_str(raw, "download_dir")),
            delete_downloads_after_request=_as_bool(
                raw.get("delete_downloads_after_request"),
                "delete_downloads_after_request",
            ),
        )


def load_config(config_path: Path | str = DEFAULT_CONFIG_PATH) -> TelegramGatewayConfig:
    return TelegramGatewayConfig.load(config_path)


def _required_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if value is None:
        raise ValueError(f"Telegram 配置缺少字段: {key}")
    text = str(value).strip()
    if not text:
        raise ValueError(f"Telegram 配置字段不能为空: {key}")
    return text


def _as_int(value: Any, key: str) -> int:
    if value is None or value == "":
        raise ValueError(f"Telegram 配置缺少字段: {key}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Telegram 配置字段必须是整数: {key}") from exc


def _as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"Telegram 配置字段必须是布尔值: {key}")


def _allowed_chat_ids(value: Any) -> set[int]:
    if not isinstance(value, list):
        raise ValueError("Telegram 配置 allowed_chat_ids 必须是数组。")

    chat_ids: set[int] = set()
    for index, item in enumerate(value):
        try:
            chat_ids.add(int(item))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Telegram 配置 allowed_chat_ids[{index}] 必须是整数。"
            ) from exc
    return chat_ids
