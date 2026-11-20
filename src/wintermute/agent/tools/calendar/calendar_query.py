"""Mainland China civil calendar and working-day arithmetic."""

import calendar
import json
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wintermute.agent import Tool, ToolContext, ToolResult

from .holidays import HolidayCalendar, YearData
from .time_query import localize


class CalendarQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["day", "month", "workday_offset"]
    date: str | None = Field(
        None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="日期或推算起点，默认北京时间今天。"
    )
    month: str | None = Field(
        None, pattern=r"^\d{4}-\d{2}$", description="month 的年月，默认北京时间本月。"
    )
    offset: int | None = Field(None, description="工作日数，1 为下一个，-1 为上一个，不计起点。")
    at_time: str | None = Field(
        None,
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="可选，工作日目标的北京时间 HH:MM，返回可创建日程的 trigger_at。",
    )

    @model_validator(mode="after")
    def check_action(self) -> Self:
        if self.month is not None and self.action != "month":
            raise ValueError("month 字段只用于月历查询")
        if self.action == "month" and self.date is not None:
            raise ValueError("月历请用 month 传入 YYYY-MM")
        if self.action == "workday_offset":
            if not self.offset:
                raise ValueError("工作日推算需要非零 offset")
        elif self.offset is not None or self.at_time is not None:
            raise ValueError("offset 和 at_time 只用于 workday_offset")
        return self


def _day_info(day: date, bulletins: list[YearData]) -> dict[str, Any]:
    info: dict[str, Any] = {"date": day.isoformat(), "iso_weekday": day.isoweekday()}
    # Next year's bulletin takes precedence over this year's for December overrides.
    for bulletin in reversed(bulletins):
        if holiday := bulletin.days.get(day):
            return {
                **info,
                "kind": "holiday" if holiday.isOffDay else "makeup_workday",
                "name": holiday.name,
                "is_workday": not holiday.isOffDay,
            }
    # A missing next-year file does not invalidate the existing year's published calendar.
    # A network failure is different: we could not check for December overrides.
    if bulletins[0].status == "no_data" or any(b.status == "unavailable" for b in bulletins):
        return {**info, "kind": "unknown", "is_workday": None}
    workday = day.weekday() < 5
    return {**info, "kind": "workday" if workday else "weekend", "is_workday": workday}


def calendar_tool(clock: Callable[[], datetime], holidays: HolidayCalendar) -> Tool[CalendarQuery]:
    china = ZoneInfo("Asia/Shanghai")

    async def execute(args: CalendarQuery, _: ToolContext) -> ToolResult:
        years: dict[int, YearData] = {}

        async def lookup(day: date) -> dict[str, Any]:
            relevant = [day.year]
            if day.month == 12 and day.year < date.max.year:
                relevant.append(day.year + 1)
            for year in relevant:
                if year not in years:
                    years[year] = await holidays.year(year)
            return _day_info(day, [years[year] for year in relevant])

        result: dict[str, Any] = {"region": "CN", "timezone": china.key}
        try:
            today = clock().astimezone(china).date()
            if args.action == "month":
                first = (
                    date.fromisoformat(args.month + "-01") if args.month else today.replace(day=1)
                )
                total = calendar.monthrange(first.year, first.month)[1]
                result["days"] = [await lookup(first.replace(day=n)) for n in range(1, total + 1)]
            else:
                day = date.fromisoformat(args.date) if args.date else today
                if args.action == "workday_offset":
                    assert args.offset is not None
                    step = timedelta(days=1 if args.offset > 0 else -1)
                    remaining = abs(args.offset)
                    while remaining:
                        day += step
                        info = await lookup(day)
                        if info["is_workday"] is None:
                            result["error"] = "缺少该日期的调休安排，无法确定目标工作日。"
                            break
                        if info["is_workday"]:
                            remaining -= 1
                    if not remaining and args.at_time is not None:
                        wall = datetime.combine(day, time.fromisoformat(args.at_time))
                        result["trigger_at"] = localize(wall, china).isoformat()
                result.update(await lookup(day))
        except (ValueError, OverflowError) as exc:
            return ToolResult(str(exc), is_error=True)

        result["bulletins"] = {
            str(year): {"status": data.status, "sources": data.papers}
            for year, data in years.items()
        }
        return ToolResult(json.dumps(result, ensure_ascii=False), is_error="error" in result)

    return Tool(
        "calendar_query",
        "查询中国大陆公历、放假及调休补班。day 查一天，month 查整月。workday_offset 用正负工作日数"
        "从指定日期推算，省略 date 从北京时间今天起算，不含起点。遇到工作日提醒先用本工具，"
        "下一个工作日上午九点可传 offset=1、at_time=09:00，再将 trigger_at 交给 schedule_write。"
        "工作日未知时向用户说明，不能假定为普通周末。",
        CalendarQuery,
        execute,
    )
