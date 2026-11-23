"""主 Agent 对当前会话日程的查询和写入。"""

import json
from datetime import datetime
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from wintermute.agent import Tool, ToolContext, ToolResult
from wintermute.events.core.schedule import ScheduleEntry, ScheduleStatus
from wintermute.events.storage.sqlite import SQLiteStore


def _parse_time(value: object) -> object:
    # Tool arguments arrive as JSON strings; Runner validates Python inputs in strict mode.
    return datetime.fromisoformat(value) if isinstance(value, str) else value


type ScheduleTime = Annotated[AwareDatetime, BeforeValidator(_parse_time)]


class _ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(
        None, min_length=1, max_length=256, description="查询或创建返回的日程 ID。"
    )


class ScheduleReadRequest(_ScheduleRequest):
    operation: Literal["list", "get"]
    start: ScheduleTime | None = Field(None, description="筛选触发时间的起点（含），须含时区。")
    end: ScheduleTime | None = Field(None, description="筛选触发时间的终点（不含），须含时区。")
    status: ScheduleStatus | None = Field(None, description="按待触发或已触发筛选。")
    limit: int | None = Field(None, ge=1, le=100, description="每页数量，默认 12。")
    cursor: str | None = Field(
        None,
        min_length=1,
        max_length=1024,
        description="继续查询时沿用筛选条件，传上一页的 next_cursor。",
    )

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        if self.operation == "get":
            if self.id is None:
                raise ValueError("get 需要日程 id")
            if any(
                v is not None for v in (self.start, self.end, self.status, self.limit, self.cursor)
            ):
                raise ValueError("get 只接受日程 id")
        elif self.id is not None:
            raise ValueError("按 ID 查询请使用 get")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("end 必须晚于 start")
        return self


class ScheduleWriteRequest(_ScheduleRequest):
    operation: Literal["create", "update", "delete"]
    title: str | None = Field(None, min_length=1, max_length=256, description="标题，create 必填。")
    content: str | None = Field(
        None, max_length=16000, description="到点交给主 Agent 的正文。传空字符串可清空。"
    )
    trigger_at: ScheduleTime | None = Field(
        None, description="create 必填，带时区的 ISO 8601 触发时间。"
    )
    action: Literal["notify_agent"] | None = Field(
        None, description="新建普通日程默认 notify_agent。设备通知用 device_messaging 创建。"
    )

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        if self.operation == "create":
            if self.id is not None:
                raise ValueError("create 不接受日程 id")
            if self.title is None or self.trigger_at is None:
                raise ValueError("创建安排需要 title 和 trigger_at")
        else:
            if self.id is None:
                raise ValueError(f"{self.operation} 需要日程 id")
            changed = any(
                v is not None for v in (self.title, self.content, self.trigger_at, self.action)
            )
            if self.operation == "update" and not changed:
                raise ValueError("更新安排至少需要一个可修改的字段")
            if self.operation == "delete" and changed:
                raise ValueError("delete 只接受日程 id")
        return self


def create_schedule_tools(
    store: SQLiteStore, conversation_id: str, timezone: str
) -> tuple[Tool[ScheduleReadRequest], Tool[ScheduleWriteRequest]]:
    user = store.get_conversation_user(conversation_id)
    zone = ZoneInfo(timezone)

    def entry_data(entry: ScheduleEntry, *, brief: bool = False) -> dict[str, Any]:
        data = entry.model_dump(mode="json", exclude={"content"} if brief else set())
        data["trigger_at"] = entry.trigger_at.astimezone(zone).isoformat()
        if entry.triggered_at is not None:
            data["triggered_at"] = entry.triggered_at.astimezone(zone).isoformat()
        return data

    def result(operation: str, **fields: Any) -> ToolResult:
        data = {
            "operation": operation,
            "timezone": timezone,
            "now": store.now_utc().astimezone(zone).isoformat(),
            **fields,
        }
        return ToolResult(json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    async def read(args: ScheduleReadRequest, _: ToolContext) -> ToolResult:
        try:
            if args.operation == "get":
                assert args.id is not None
                item = store.schedules.get_schedule(
                    user_id=user, conversation_id=conversation_id, id=args.id
                )
                return result("get", item=entry_data(item))
            items, cursor = store.schedules.list_schedules(
                user_id=user,
                conversation_id=conversation_id,
                start=args.start,
                end=args.end,
                status=args.status,
                limit=args.limit or 12,
                cursor=args.cursor,
            )
            return result(
                "list", items=[entry_data(item, brief=True) for item in items], next_cursor=cursor
            )
        except LookupError:
            return ToolResult("本会话中找不到这项安排。", is_error=True)
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)

    async def write(args: ScheduleWriteRequest, _: ToolContext) -> ToolResult:
        try:
            if args.operation == "create":
                assert args.title is not None and args.trigger_at is not None
                item = store.schedules.create_schedule(
                    user_id=user,
                    conversation_id=conversation_id,
                    title=args.title,
                    content=args.content or "",
                    trigger_at=args.trigger_at,
                    action=args.action or "notify_agent",
                )
            else:
                assert args.id is not None
                if args.operation == "delete":
                    item = store.schedules.delete_schedule(
                        user_id=user, conversation_id=conversation_id, id=args.id
                    )
                else:
                    item = store.schedules.update_schedule(
                        user_id=user,
                        conversation_id=conversation_id,
                        id=args.id,
                        title=args.title,
                        content=args.content,
                        trigger_at=args.trigger_at,
                        action=args.action,
                    )
            return result(args.operation, item=entry_data(item))
        except LookupError:
            return ToolResult("本会话中找不到这项安排。", is_error=True)
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)

    return (
        Tool(
            "schedule_read",
            f"查询本会话的时间表，默认时区 {timezone}。list 分页查询安排，get 按 id 读取完整正文。"
            "单查时间用 time_query，查询工作日用 calendar_query。",
            ScheduleReadRequest,
            read,
        ),
        Tool(
            "schedule_write",
            "修改本会话的时间表。create 新建单次安排，update 修改待触发安排，delete 删除安排。"
            "用 schedule_read 查询安排和 id。工作日提醒先用 calendar_query 推算 trigger_at。"
            "普通日程到点后主 Agent 会收到标题和正文。设备通知也可以按 id 改期或删除。",
            ScheduleWriteRequest,
            write,
        ),
    )
