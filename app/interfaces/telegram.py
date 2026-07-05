from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..flows.flow_runtime import FlowSubmitRequest, FlowSubmitResult, InterfaceOutput

logger = logging.getLogger(__name__)


class TelegramAdapter:
    """Telegram Bot API 长轮询适配器。"""

    def __init__(
        self,
        *,
        name: str,
        bot_token: str,
        input_level: str | None = None,
        allowed_chat_ids: tuple[str, ...] = (),
        poll_interval_seconds: float = 1.0,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        self.name = name
        self.bot_token = bot_token
        self.input_level = str(input_level).strip().upper() if input_level is not None else None
        self.allowed_chat_ids = {str(item).strip() for item in allowed_chat_ids if str(item).strip()}
        self.poll_interval_seconds = max(0.1, poll_interval_seconds)
        self.request_timeout_seconds = max(1.0, request_timeout_seconds)
        self._submit: Callable[[FlowSubmitRequest], FlowSubmitResult] | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset: int | None = None

    def start(self, submit: Callable[[FlowSubmitRequest], FlowSubmitResult]) -> None:
        """启动 Telegram 长轮询；仅输出接口不会启动轮询线程。"""
        self._submit = submit
        if self.input_level is None or self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name=f"wintermute-{self.name}-telegram",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """停止长轮询线程。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.request_timeout_seconds + 2.0)
            self._thread = None

    def send(self, output: InterfaceOutput) -> None:
        """向 Telegram chat 发送文本消息。"""
        chat_id = str(output.target.get("chat_id") or "").strip()
        self._api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": output.message,
            },
        )

    def request_from_update(self, update: dict[str, Any]) -> FlowSubmitRequest | None:
        """把 Telegram update 转成流程输入。"""
        if self.input_level is None:
            return None
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return None
        chat_id = str(chat.get("id") or "").strip()
        if not chat_id or (self.allowed_chat_ids and chat_id not in self.allowed_chat_ids):
            return None

        text = _clean_str(message.get("text")) or _clean_str(message.get("caption"))
        attachments = self._attachments_from_message(message)

        metadata = {
            "telegram": {
                "update_id": update.get("update_id"),
                "message_id": message.get("message_id"),
                "chat_id": chat_id,
            }
        }
        return FlowSubmitRequest(
            level=self.input_level,
            message=text,
            attachments=attachments,
            source="telegram",
            input_interface=self.name,
            reply_target={"chat_id": chat_id},
            metadata=metadata,
        )

    def _poll_loop(self) -> None:
        assert self._submit is not None
        while not self._stop_event.is_set():
            try:
                updates = self._api(
                    "getUpdates",
                    {
                        "timeout": int(self.request_timeout_seconds),
                        **({"offset": self._offset} if self._offset is not None else {}),
                    },
                    timeout=self.request_timeout_seconds + 5.0,
                )
                if not isinstance(updates, list):
                    logger.warning("Telegram getUpdates 返回非数组 result")
                    continue
                for update in updates:
                    if not isinstance(update, dict):
                        continue
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._offset = update_id + 1
                    request = self.request_from_update(update)
                    if request is None:
                        continue
                    result = self._submit(request)
                    if result.status == "error":
                        logger.error("Telegram 输入提交失败 level=%s error=%s", result.level, result.error)
            except Exception:  # noqa: BLE001 - 长轮询失败后继续重试
                logger.exception("Telegram 长轮询失败 name=%s", self.name)
                if self._stop_event.wait(self.poll_interval_seconds):
                    return

    def _attachments_from_message(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        photo = message.get("photo")
        if isinstance(photo, list) and photo:
            selected = _largest_photo(photo)
            attachment = self._attachment_from_file("image", selected)
            if attachment is not None:
                attachments.append(attachment)

        for key, kind in (
            ("voice", "audio"),
            ("audio", "audio"),
            ("video", "video"),
            ("document", "file"),
        ):
            value = message.get(key)
            if isinstance(value, dict):
                attachment = self._attachment_from_file(kind, value)
                if attachment is not None:
                    attachments.append(attachment)
        return attachments

    def _attachment_from_file(
        self,
        kind: str,
        item: dict[str, Any],
    ) -> dict[str, Any] | None:
        file_id = _clean_str(item.get("file_id"))
        if not file_id:
            return None
        try:
            url = self._file_url(file_id)
        except Exception:  # noqa: BLE001 - 单个附件失败不丢弃整条消息
            logger.exception("Telegram 文件 URL 获取失败 file_id=%s", file_id)
            return None
        attachment: dict[str, Any] = {"kind": kind, "url": url}
        mime_type = _clean_str(item.get("mime_type"))
        if mime_type:
            attachment["mime_type"] = mime_type
        filename = _clean_str(item.get("file_name"))
        if filename:
            attachment["filename"] = filename
        return attachment

    def _file_url(self, file_id: str) -> str:
        result = self._api("getFile", {"file_id": file_id})
        if not isinstance(result, dict):
            raise RuntimeError("Telegram getFile 返回非对象 result")
        file_path = _clean_str(result.get("file_path"))
        if not file_path:
            raise RuntimeError("Telegram getFile 缺少 file_path")
        return f"https://api.telegram.org/file/bot{self.bot_token}/{quote(file_path, safe='/')}"

    def _api(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        body = urlencode({key: value for key, value in params.items() if value is not None}).encode(
            "utf-8"
        )
        request = Request(
            f"https://api.telegram.org/bot{self.bot_token}/{method}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(request, timeout=timeout or self.request_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(f"Telegram API 调用失败: {method}")
        return payload.get("result")


def _largest_photo(items: list[Any]) -> dict[str, Any]:
    photos = [item for item in items if isinstance(item, dict)]
    if not photos:
        return {}
    return max(
        photos,
        key=lambda item: (
            int(item.get("file_size") or 0),
            int(item.get("width") or 0) * int(item.get("height") or 0),
        ),
    )


def _clean_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
