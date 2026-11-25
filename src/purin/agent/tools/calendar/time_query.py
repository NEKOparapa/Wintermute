"""Local clock and IANA time-zone conversions."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from purin.agent import Tool, ToolContext, ToolResult


class TimeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["now", "convert"] = "now"
    timezone: str | None = Field(None, description="目标 IANA 时区，默认使用后端配置时区。")
    at: str | None = Field(None, description="convert 的 ISO 8601 日期时间，可带 UTC 偏移。")
    from_timezone: str | None = Field(None, description="at 不带偏移时使用的 IANA 时区。")

    @model_validator(mode="after")
    def check_action(self) -> Self:
        if self.action == "now" and (self.at is not None or self.from_timezone is not None):
            raise ValueError("now 只接受目标 timezone")
        if self.action == "convert" and self.at is None:
            raise ValueError("convert 需要 at")
        return self


def localize(wall: datetime, zone: ZoneInfo) -> datetime:
    # Only UTC round-trips distinguish missing and repeated local wall times.
    instants = set()
    for fold in (0, 1):
        instant = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == wall:
            instants.add(instant)
    if not instants:
        raise ValueError("这个当地时间因时钟跳变而不存在")
    if len(instants) > 1:
        raise ValueError("这个当地时间有两个可能的时刻，请在 at 中明确 UTC 偏移")
    return instants.pop().astimezone(zone)


def time_tool(clock: Callable[[], datetime], default_zone: ZoneInfo) -> Tool[TimeQuery]:
    async def execute(args: TimeQuery, _: ToolContext) -> ToolResult:
        try:
            target = ZoneInfo(args.timezone) if args.timezone is not None else default_zone
            if args.action == "now":
                instant = clock()
            else:
                assert args.at is not None
                instant = datetime.fromisoformat(args.at)
                if instant.tzinfo is None:
                    source = (
                        ZoneInfo(args.from_timezone)
                        if args.from_timezone is not None
                        else default_zone
                    )
                    instant = localize(instant, source)
                elif args.from_timezone is not None:
                    raise ValueError("at 已含 UTC 偏移时请省略 from_timezone")
            local = instant.astimezone(target)
            return ToolResult(
                json.dumps(
                    {
                        "datetime": local.isoformat(),
                        "date": local.date().isoformat(),
                        "timezone": target.key,
                        "utc": instant.astimezone(UTC).isoformat(),
                        "iso_weekday": local.isoweekday(),
                    },
                    ensure_ascii=False,
                )
            )
        except (ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
            return ToolResult(str(exc), is_error=True)

    return Tool(
        "time_query",
        f"本地查询当前时间（now）或进行时区换算（convert）。默认时区 {default_zone.key}。"
        "返回含 UTC 偏移的时间、日期和星期（周一为 1）。",
        TimeQuery,
        execute,
    )
