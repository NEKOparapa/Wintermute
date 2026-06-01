from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .attention.attention import AttentionChannel, AttentionLevel, parse_level, route_event
from .dialogue import DialogueService
from .event.event import normalize_event
from .ingest import EventIngestService
from .llm.llm import LLMError

logger = logging.getLogger(__name__)

_DIALOGUE_LEVELS = {AttentionLevel.L0, AttentionLevel.L1}


def build_http_server(
    service: DialogueService,
    ingest_service: EventIngestService,
    host: str,
    port: int,
) -> ThreadingHTTPServer:
    """创建 HTTP 服务，并把对话服务与背景事件摄入服务绑定到请求处理器上。"""
    class Handler(WintermuteRequestHandler):
        dialogue_service = service
        event_ingest_service = ingest_service

    return ThreadingHTTPServer((host, port), Handler)


class WintermuteRequestHandler(BaseHTTPRequestHandler):
    """HTTP 输入入口：把 /event 请求转成标准事件并进入注意力层。"""

    dialogue_service: DialogueService
    event_ingest_service: EventIngestService

    def do_GET(self) -> None:
        """处理健康检查接口，目前只支持 GET /health。"""
        if self.path != "/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:
        """处理事件接口 POST /event。

        请求体支持：
        - message / attachments：事件内容（二者不能同时为空）。
        - source：事件源头，默认 "user"（如 sensor:door / calendar）。
        - level：注意力等级 L0–L3，默认 L0；由调用方指定。
        - type：事件类型，默认对话事件 user_message、背景事件 observation。

        L0/L1 进入会话流程唤起主 AI 对话；L2/L3 进入背景流程，只落库并逐条压缩进事件记忆。
        """
        if self.path != "/event":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        try:
            # 1. 读取并校验 JSON 请求体。
            payload = self._read_json_body()
            message = payload.get("message")
            attachments = payload.get("attachments")
            has_text = isinstance(message, str) and message.strip()
            has_attachments = isinstance(attachments, list) and len(attachments) > 0
            if not has_text and not has_attachments:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "message 与 attachments 不能同时为空。"},
                )
                return

            # 2. 解析源头与注意力等级（等级非法会抛 ValueError → 400）。
            level = parse_level(payload.get("level") if payload.get("level") is not None else "L0")
            source = _clean_field(payload.get("source")) or "user"
            event_type = _clean_field(payload.get("type")) or _default_type(level)

            # 3. 把输入归一化成带源头与等级的标准事件，并进入注意力层路由。
            event = normalize_event(
                message if isinstance(message, str) else None,
                attachments,
                source=source,
                type=event_type,
                attention_level=level.value,
            )
            route = route_event(event)

            # 4. 按通道分流：会话事件唤起对话，背景事件只摄入并压缩进记忆。
            if route.channel == AttentionChannel.DIALOGUE:
                result = self.dialogue_service.handle_event(route.event)
                self._send_json(HTTPStatus.OK, {"message": result.message})
            else:
                ingested = self.event_ingest_service.handle_event(route.event)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "accepted",
                        "level": ingested.level,
                        "type": ingested.type,
                        "event_id": ingested.event_id,
                    },
                )
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "请求体必须是 JSON 对象。"})
        except LLMError as exc:
            logger.exception("LLM 请求失败")
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            logger.exception("HTTP 请求处理失败")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_server_error"})

    def log_message(self, format: str, *args: Any) -> None:
        """把 http.server 默认访问日志转到项目 logging。"""
        logger.info("%s - %s", self.address_string(), format % args)

    def _read_json_body(self) -> dict[str, Any]:
        """读取并校验 JSON 请求体，要求顶层必须是对象。"""
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length).decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise json.JSONDecodeError("JSON body must be object", raw, 0)
        return data

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        """用统一格式返回 JSON 响应。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _default_type(level: AttentionLevel) -> str:
    """未显式给出 type 时，按通道选择默认类型：会话事件 user_message，背景事件 observation。"""
    return "user_message" if level in _DIALOGUE_LEVELS else "observation"


def _clean_field(value: Any) -> str | None:
    """把可选字符串字段规范成非空字符串或 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
