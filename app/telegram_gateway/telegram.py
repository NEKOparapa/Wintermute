from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError


TELEGRAM_MESSAGE_LIMIT = 4096


class TelegramAPIError(RuntimeError):
    """Telegram Bot API request failed."""


class TelegramClient:
    """Small Telegram Bot API client backed by urllib."""

    def __init__(self, bot_token: str, *, timeout_seconds: int = 30) -> None:
        self.bot_token = bot_token
        self.timeout_seconds = timeout_seconds

    def set_webhook(self, webhook_url: str, secret_token: str) -> dict[str, Any]:
        return self._api_request(
            "setWebhook",
            {
                "url": webhook_url,
                "secret_token": secret_token,
                "allowed_updates": ["message"],
            },
        )

    def get_file(self, file_id: str) -> dict[str, Any]:
        result = self._api_request("getFile", {"file_id": file_id})
        if not isinstance(result, dict):
            raise TelegramAPIError("Telegram getFile 响应格式错误。")
        file_path = str(result.get("file_path") or "").strip()
        if not file_path:
            raise TelegramAPIError("Telegram getFile 响应缺少 file_path。")
        return result

    def download_file(self, file_path: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
        try:
            with request.urlopen(url, timeout=self.timeout_seconds) as response:
                with destination.open("wb") as file:
                    shutil.copyfileobj(response, file)
        except (OSError, HTTPError, URLError) as exc:
            raise TelegramAPIError("Telegram 文件下载失败。") from exc
        return destination

    def send_message(self, chat_id: int, text: str) -> None:
        for chunk in split_message(text):
            self._api_request("sendMessage", {"chat_id": chat_id, "text": chunk})

    def _api_request(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except (HTTPError, URLError) as exc:
            raise TelegramAPIError(f"Telegram API 请求失败: {method}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TelegramAPIError(f"Telegram API 响应不是 JSON: {method}") from exc

        if not isinstance(data, dict) or data.get("ok") is not True:
            description = ""
            if isinstance(data, dict):
                description = str(data.get("description") or "")
            suffix = f": {description}" if description else ""
            raise TelegramAPIError(f"Telegram API 请求失败: {method}{suffix}")
        return data.get("result")


def split_message(text: str, *, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return [""]
    return [text[index : index + limit] for index in range(0, len(text), limit)]
