from __future__ import annotations

import json
from typing import Any

from app.infrastructure.storage.schedule_store import ScheduleStore
from .base import Tool

SCHEDULE_ACTIONS = {"create", "list", "get", "update", "delete"}

_RECURRENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "重复规则；不重复可省略或 frequency=none。",
    "properties": {
        "frequency": {
            "type": "string",
            "enum": ["none", "daily", "weekly", "monthly"],
            "description": "重复频率。",
        },
        "interval": {
            "type": "integer",
            "description": "重复间隔，默认 1。",
        },
        "until": {
            "type": "string",
            "description": "可选 ISO 8601 截止时间。",
        },
    },
    "additionalProperties": False,
}


class ScheduleTool(Tool):
    """统一日程表工具，通过 action 执行增删改查。"""

    name = "schedule"
    description = (
        "日程表工具。通过 action 执行 create/list/get/update/delete。"
        "trigger_at、start、end、recurrence.until 必须使用 ISO 8601 日期时间。"
        "delete 为软删除，会把日程状态标记为 cancelled。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "get", "update", "delete"],
                "description": "要执行的日程操作。",
            },
            "id": {
                "type": "string",
                "description": "日程 id；get/update/delete 必填。",
            },
            "title": {
                "type": "string",
                "description": "日程标题；create 必填，update 可选。",
            },
            "content": {
                "type": "string",
                "description": "日程内容；create/update 可选。",
            },
            "trigger_at": {
                "type": "string",
                "description": "日程触发时间；create 必填，update 可选。",
            },
            "recurrence": _RECURRENCE_SCHEMA,
            "status": {
                "type": "string",
                "enum": ["active", "completed", "cancelled"],
                "description": "新状态；update 可选。",
            },
            "start": {
                "type": "string",
                "description": "list 的可选 ISO 8601 起始时间，包含。",
            },
            "end": {
                "type": "string",
                "description": "list 的可选 ISO 8601 结束时间，不包含。",
            },
            "include_cancelled": {
                "type": "boolean",
                "description": "list 未指定 status 时，是否包含已取消日程。",
            },
            "limit": {
                "type": "integer",
                "description": "list 最多返回条数。",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        store: ScheduleStore,
        *,
        allowed_actions: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.store = store
        self.allowed_actions = frozenset(allowed_actions or SCHEDULE_ACTIONS)

    def run(self, arguments: dict[str, Any]) -> str:
        action = str(arguments.get("action") or "").strip().lower()
        if action not in SCHEDULE_ACTIONS:
            return _error("unknown_action", action=action)
        if action not in self.allowed_actions:
            return _error("action_not_allowed", action=action)

        try:
            if action == "create":
                return self._create(arguments)
            if action == "list":
                return self._list(arguments)
            if action == "get":
                return self._get(arguments)
            if action == "update":
                return self._update(arguments)
            if action == "delete":
                return self._delete(arguments)
        except Exception as exc:  # noqa: BLE001 - 工具错误需要稳定 JSON 返回
            return _error(str(exc))
        return _error("unknown_action", action=action)

    def _create(self, arguments: dict[str, Any]) -> str:
        schedule = self.store.create_schedule(
            title=arguments.get("title"),
            content=arguments.get("content", ""),
            trigger_at=arguments.get("trigger_at"),
            recurrence=arguments.get("recurrence"),
        )
        return _json({"schedule": schedule})

    def _list(self, arguments: dict[str, Any]) -> str:
        limit = _optional_int(arguments.get("limit"))
        schedules = self.store.list_schedules(
            status=arguments.get("status"),
            start=arguments.get("start"),
            end=arguments.get("end"),
            include_cancelled=_optional_bool(arguments.get("include_cancelled"), default=False),
            limit=limit,
        )
        return _json({"count": len(schedules), "schedules": schedules})

    def _get(self, arguments: dict[str, Any]) -> str:
        schedule = self.store.get_schedule(arguments.get("id"))
        if schedule is None:
            return _error("schedule_not_found", id=arguments.get("id"))
        return _json({"schedule": schedule})

    def _update(self, arguments: dict[str, Any]) -> str:
        changes = {
            key: arguments[key]
            for key in ("title", "content", "trigger_at", "recurrence", "status")
            if key in arguments
        }
        if not changes:
            return _error("missing_updates")
        schedule = self.store.update_schedule(arguments.get("id"), changes)
        if schedule is None:
            return _error("schedule_not_found", id=arguments.get("id"))
        return _json({"schedule": schedule})

    def _delete(self, arguments: dict[str, Any]) -> str:
        schedule = self.store.delete_schedule(arguments.get("id"))
        if schedule is None:
            return _error("schedule_not_found", id=arguments.get("id"))
        return _json({"schedule": schedule})


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_bool(value: object, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(message: str, **extra: Any) -> str:
    return _json({"error": message, **extra})
