from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from .base import ToolRegistry

logger = logging.getLogger(__name__)


class ToolCallLike(Protocol):
    """工具执行只依赖模型返回的工具名和原始 JSON 参数。"""

    name: str
    arguments: str


def run_registered_tool(
    registry: ToolRegistry | None,
    call: ToolCallLike,
    context: Any | None = None,
) -> str:
    """执行注册工具；支持可选调用上下文，所有失败均包装为 JSON。"""
    tool = registry.get(call.name) if registry is not None else None
    if tool is None:
        return json.dumps(
            {"error": f"unknown_tool: {call.name}"},
            ensure_ascii=False,
        )

    try:
        arguments = json.loads(call.arguments) if call.arguments else {}
    except json.JSONDecodeError:
        return json.dumps(
            {"error": "invalid_arguments_json", "raw": call.arguments},
            ensure_ascii=False,
        )
    if not isinstance(arguments, dict):
        return json.dumps(
            {"error": "arguments_not_object"},
            ensure_ascii=False,
        )

    try:
        run_with_context = getattr(tool, "run_with_context", None)
        if callable(run_with_context):
            return run_with_context(arguments, context)
        return tool.run(arguments)
    except Exception as exc:  # noqa: BLE001 - 工具异常不应中断流程
        logger.exception("工具执行异常 name=%s", call.name)
        return json.dumps(
            {"error": "tool_exception", "message": str(exc)},
            ensure_ascii=False,
        )
