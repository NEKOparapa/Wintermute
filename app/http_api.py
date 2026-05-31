from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .attention.attention import AttentionLevel, route_event
from .dialogue import DialogueService
from .event.event import normalize_message_event
from .llm.llm import LLMError

logger = logging.getLogger(__name__)


def build_http_server(service: DialogueService, host: str, port: int) -> ThreadingHTTPServer:
    """创建 HTTP 服务，并把同一个 DialogueService 绑定到请求处理器上。"""
    class Handler(WintermuteRequestHandler):
        dialogue_service = service

    return ThreadingHTTPServer((host, port), Handler)


class WintermuteRequestHandler(BaseHTTPRequestHandler):
    """HTTP 输入入口：把 /event 请求转成标准事件并进入注意力层。"""

    dialogue_service: DialogueService

    def do_GET(self) -> None:
        """处理健康检查接口，目前只支持 GET /health。"""
        if self.path != "/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:
        """处理事件接口 POST /event，请求体只需要包含 message。"""
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

            # 2. 把用户输入（含多模态附件）归一化成事件，并进入注意力层路由。
            event = normalize_message_event(
                message if isinstance(message, str) else None,
                attachments,
            )

            # 3. 注意力层会把事件路由到不同的处理器，目前只有 DialogueService。
            route = route_event(event)

            # 4. 调用处理器并返回结果。
            result = self.dialogue_service.handle_event(route.event)

            # 5. 把处理结果以统一格式返回 JSON 响应。
            self._send_json(
                HTTPStatus.OK,
                {
                    "message": result.message,
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
