from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

from .config import TelegramGatewayConfig
from .telegram import TelegramClient
from .wintermute import WintermuteClient

logger = logging.getLogger(__name__)

WINTERMUTE_UNAVAILABLE_MESSAGE = "Wintermute 当前不可用，请稍后再试。"


@dataclass(frozen=True)
class TelegramAttachment:
    kind: str
    path: Path
    mime_type: str

    def to_wintermute_attachment(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "path": str(self.path),
            "mime_type": self.mime_type,
        }


class TelegramGateway:
    """Receives Telegram updates and forwards supported messages to Wintermute."""

    def __init__(
        self,
        config: TelegramGatewayConfig,
        telegram: TelegramClient,
        wintermute: WintermuteClient,
    ) -> None:
        self.config = config
        self.telegram = telegram
        self.wintermute = wintermute
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._run_worker,
            name="telegram-gateway-worker",
            daemon=True,
        )
        self._worker.start()

    def stop(self) -> None:
        if self._worker is None:
            return
        self._queue.put(None)
        self._worker.join(timeout=5)
        self._worker = None

    def queue_size(self) -> int:
        return self._queue.qsize()

    def enqueue_update(self, update: dict[str, Any]) -> None:
        self._queue.put(update)

    def process_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat_id = _message_chat_id(message)
        if chat_id is None:
            return
        if chat_id not in self.config.allowed_chat_ids:
            logger.info("忽略未授权 Telegram chat_id=%s", chat_id)
            return

        downloaded: list[Path] = []
        try:
            payload, downloaded = self._build_wintermute_payload(update, message, chat_id)
            if payload is None:
                logger.info("忽略不支持的 Telegram 消息 chat_id=%s", chat_id)
                return
            reply = self.wintermute.send_event(payload)
        except Exception:
            logger.exception("Telegram 消息转发到 Wintermute 失败 chat_id=%s", chat_id)
            reply = WINTERMUTE_UNAVAILABLE_MESSAGE
        finally:
            if self.config.delete_downloads_after_request:
                _delete_downloaded_files(downloaded)

        try:
            self.telegram.send_message(chat_id, reply)
        except Exception:
            logger.exception("Telegram 回复发送失败 chat_id=%s", chat_id)

    def _run_worker(self) -> None:
        while True:
            update = self._queue.get()
            try:
                if update is None:
                    return
                self.process_update(update)
            finally:
                self._queue.task_done()

    def _build_wintermute_payload(
        self,
        update: dict[str, Any],
        message: dict[str, Any],
        chat_id: int,
    ) -> tuple[dict[str, Any] | None, list[Path]]:
        attachments: list[TelegramAttachment] = []
        downloaded: list[Path] = []

        voice = message.get("voice")
        if isinstance(voice, dict):
            attachment = self._download_voice(update, voice, chat_id)
            attachments.append(attachment)
            downloaded.append(attachment.path)

        photo = _largest_photo(message.get("photo"))
        if photo is not None:
            attachment = self._download_photo(update, photo, chat_id)
            attachments.append(attachment)
            downloaded.append(attachment.path)

        text = _message_text(message, has_voice=voice is not None, has_photo=photo is not None)
        if not text and not attachments:
            return None, downloaded

        payload: dict[str, Any] = {
            "source": f"telegram:{chat_id}",
            "level": "L0",
            "type": "user_message",
            "message": text,
        }
        if attachments:
            payload["attachments"] = [
                attachment.to_wintermute_attachment() for attachment in attachments
            ]
        return payload, downloaded

    def _download_voice(
        self,
        update: dict[str, Any],
        voice: dict[str, Any],
        chat_id: int,
    ) -> TelegramAttachment:
        file_id = _required_file_id(voice, "voice")
        file_path = str(self.telegram.get_file(file_id)["file_path"])
        destination = _download_destination(
            self.config.download_dir,
            chat_id,
            update,
            file_id,
            file_path,
            fallback_suffix=".ogg",
        )
        self.telegram.download_file(file_path, destination)
        return TelegramAttachment(kind="audio", path=destination, mime_type="audio/ogg")

    def _download_photo(
        self,
        update: dict[str, Any],
        photo: dict[str, Any],
        chat_id: int,
    ) -> TelegramAttachment:
        file_id = _required_file_id(photo, "photo")
        file_path = str(self.telegram.get_file(file_id)["file_path"])
        destination = _download_destination(
            self.config.download_dir,
            chat_id,
            update,
            file_id,
            file_path,
            fallback_suffix=".jpg",
        )
        self.telegram.download_file(file_path, destination)
        return TelegramAttachment(kind="image", path=destination, mime_type="image/jpeg")


def build_http_server(gateway: TelegramGateway) -> ThreadingHTTPServer:
    class Handler(TelegramWebhookRequestHandler):
        telegram_gateway = gateway

    return ThreadingHTTPServer((gateway.config.host, gateway.config.port), Handler)


class TelegramWebhookRequestHandler(BaseHTTPRequestHandler):
    telegram_gateway: TelegramGateway

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._send_json(
            HTTPStatus.OK,
            {"status": "ok", "queue_size": self.telegram_gateway.queue_size()},
        )

    def do_POST(self) -> None:
        gateway = self.telegram_gateway
        if self.path != gateway.config.path:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != (
            gateway.config.webhook_secret_token
        ):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return

        try:
            payload = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return

        gateway.enqueue_update(payload)
        self._send_json(HTTPStatus.OK, {"status": "accepted"})

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    def _read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length).decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise json.JSONDecodeError("JSON body must be object", raw, 0)
        return data

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _message_chat_id(message: dict[str, Any]) -> int | None:
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    try:
        return int(chat["id"])
    except (KeyError, TypeError, ValueError):
        return None


def _message_text(message: dict[str, Any], *, has_voice: bool, has_photo: bool) -> str:
    text = _clean_text(message.get("text"))
    if text:
        return text

    caption = _clean_text(message.get("caption"))
    if caption:
        return caption

    if has_voice and has_photo:
        return "用户发来一段语音和一张图片，请理解后回复。"
    if has_voice:
        return "用户发来一段语音，请理解后回复。"
    if has_photo:
        return "用户发来一张图片，请理解后回复。"
    return ""


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _largest_photo(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, list) or not value:
        return None
    photos = [item for item in value if isinstance(item, dict)]
    if not photos:
        return None
    return max(photos, key=_photo_sort_key)


def _photo_sort_key(photo: dict[str, Any]) -> tuple[int, int]:
    file_size = _safe_int(photo.get("file_size"))
    width = _safe_int(photo.get("width"))
    height = _safe_int(photo.get("height"))
    return (file_size, width * height)


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _required_file_id(item: dict[str, Any], kind: str) -> str:
    file_id = _clean_text(item.get("file_id"))
    if not file_id:
        raise ValueError(f"Telegram {kind} 消息缺少 file_id。")
    return file_id


def _download_destination(
    download_dir: Path,
    chat_id: int,
    update: dict[str, Any],
    file_id: str,
    file_path: str,
    *,
    fallback_suffix: str,
) -> Path:
    suffix = PurePosixPath(file_path).suffix or fallback_suffix
    update_id = _safe_int(update.get("update_id"))
    name = f"{update_id}_{_sanitize_filename(file_id)}{suffix}"
    return download_dir / str(chat_id) / name


def _sanitize_filename(value: str) -> str:
    chars = []
    for char in value:
        if char.isalnum() or char in {"-", "_"}:
            chars.append(char)
        else:
            chars.append("_")
    return "".join(chars) or "file"


def _delete_downloaded_files(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Telegram 临时附件删除失败 path=%s", path)
