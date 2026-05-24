from __future__ import annotations

import json
import logging
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .attention.attention import AttentionLevel, route_event
from .dialogue import DialogueService
from .event.event import normalize_message_event
from .llm.llm import LLMError
from .memory.orchestrator import MemoryOrchestrator

logger = logging.getLogger(__name__)


def build_http_server(
    service: DialogueService,
    orchestrator: MemoryOrchestrator,
    host: str,
    port: int,
) -> ThreadingHTTPServer:
    """创建 HTTP 服务,把 DialogueService 和 MemoryOrchestrator 绑定到请求处理器。"""

    class Handler(WintermuteRequestHandler):
        dialogue_service = service
        memory_orchestrator = orchestrator

    return ThreadingHTTPServer((host, port), Handler)


class WintermuteRequestHandler(BaseHTTPRequestHandler):
    """HTTP 输入入口:把 HTTP 请求转成业务调用。"""

    dialogue_service: DialogueService
    memory_orchestrator: MemoryOrchestrator

    def do_GET(self) -> None:
        """处理健康检查接口,目前只支持 GET /health。"""
        if self.path != "/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:
        """根据路径分发 POST 请求。"""
        if self.path == "/event":
            self._handle_event()
            return
        if self.path == "/memory/rollup":
            self._handle_memory_rollup()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    # ------------------------------------------------------------------ /event

    def _handle_event(self) -> None:
        """处理 POST /event:用户消息进入注意力层与对话流程。"""
        try:
            payload = self._read_json_body()
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "message 不能为空。"})
                return

            event = normalize_message_event(message)
            route = route_event(event)
            if route is None or route.level is not AttentionLevel.L0:
                raise ValueError("事件暂不支持。")

            result = self.dialogue_service.handle_event(route.event)
            self._send_json(HTTPStatus.OK, {"message": result.message})
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

    # ------------------------------------------------------ /memory/rollup

    def _handle_memory_rollup(self) -> None:
        """处理 POST /memory/rollup:手动触发某层级的压缩,方便测试与回填。

        请求体示例:
            {"kind": "daily", "date": "2026-05-23"}
            {"kind": "weekly", "year": 2026, "week": 21}
            {"kind": "monthly", "year": 2026, "month": 5}
        """
        try:
            payload = self._read_json_body()
            kind = payload.get("kind")
            entry = self._dispatch_rollup(kind, payload)
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "请求体必须是 JSON 对象。"})
            return
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception:
            logger.exception("memory rollup 处理失败")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_server_error"})
            return

        if entry is None:
            self._send_json(HTTPStatus.OK, {"created": False})
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "created": True,
                "memory_id": entry.id,
                "kind": entry.kind.value,
                "summary_tokens": entry.tokens,
            },
        )

    def _dispatch_rollup(self, kind: Any, payload: dict[str, Any]):
        """按 kind 调用 orchestrator 对应的 rollup 方法,参数缺失抛 ValueError。"""
        if kind == "daily":
            target_date = _parse_date(payload.get("date"))
            return self.memory_orchestrator.rollup_daily(target_date)
        if kind == "weekly":
            year = _parse_int(payload.get("year"), "year")
            week = _parse_int(payload.get("week"), "week")
            return self.memory_orchestrator.rollup_weekly(year, week)
        if kind == "monthly":
            year = _parse_int(payload.get("year"), "year")
            month = _parse_int(payload.get("month"), "month")
            return self.memory_orchestrator.rollup_monthly(year, month)
        raise ValueError("kind 必须是 daily / weekly / monthly 之一。")

    # ------------------------------------------------------------------- 通用

    def log_message(self, format: str, *args: Any) -> None:
        """把 http.server 默认访问日志转到项目 logging。"""
        logger.info("%s - %s", self.address_string(), format % args)

    def _read_json_body(self) -> dict[str, Any]:
        """读取并校验 JSON 请求体,要求顶层必须是对象。"""
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


# ============================================================== 解析辅助


def _parse_date(raw: Any) -> date:
    """把请求体中的 date 字段解析成 date,无效或缺失抛 ValueError。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("date 字段必须是 YYYY-MM-DD 字符串。")
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError(f"date 字段格式错误: {raw}") from exc


def _parse_int(raw: Any, field_name: str) -> int:
    """把请求体中的整数字段解析成 int,无效或缺失抛 ValueError。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{field_name} 字段必须是整数。")
    return raw
