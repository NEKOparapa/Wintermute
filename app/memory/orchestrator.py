from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from ..storage.storage import GlobalEventStore
from .consolidator import Consolidator
from .memory import MemoryEntry, MemoryKind, MemoryStore

logger = logging.getLogger(__name__)


class MemoryOrchestrator:
    """协调多层级记忆的生成流程,负责:
    1) 按层级查找输入数据 (events / 下层 memories);
    2) 检查幂等键,已存在则跳过;
    3) 调 Consolidator 产出新条目并写入 MemoryStore。
    """

    def __init__(
        self,
        events_store: GlobalEventStore,
        memory_store: MemoryStore,
        consolidator: Consolidator,
    ) -> None:
        """注入存储和压缩器,所有层级共用同一组依赖。"""
        self.events_store = events_store
        self.memory_store = memory_store
        self.consolidator = consolidator

    # -------------------------------------------------------------------- daily

    def rollup_daily(self, target_date: date) -> MemoryEntry | None:
        """压缩 target_date 当天的全部原始事件成一条 daily 记忆。"""
        period_start, period_end = _day_period(target_date)
        if self.memory_store.has_for_period(MemoryKind.DAILY, period_start, period_end):
            logger.info("daily 跳过:已存在 date=%s", target_date)
            return None

        events = self.events_store.load_events_for_date(target_date)
        if not events:
            logger.info("daily 跳过:当日无事件 date=%s", target_date)
            return None

        try:
            entry = self.consolidator.compress_to_daily(
                events,
                period_start=period_start,
                period_end=period_end,
            )
        except Exception:
            logger.exception("daily 压缩失败 date=%s events=%d", target_date, len(events))
            return None

        self.memory_store.append(entry)
        logger.info(
            "daily 压缩完成 date=%s events=%d summary_tokens=%d",
            target_date,
            len(events),
            entry.tokens,
        )
        return entry

    # ------------------------------------------------------------------- weekly

    def rollup_weekly(self, iso_year: int, iso_week: int) -> MemoryEntry | None:
        """把一个 ISO 周内的 daily 记忆合并成一条 weekly 记忆。"""
        period_start, period_end = _iso_week_period(iso_year, iso_week)
        if self.memory_store.has_for_period(MemoryKind.WEEKLY, period_start, period_end):
            logger.info("weekly 跳过:已存在 %04d-W%02d", iso_year, iso_week)
            return None

        in_week = _select_in_period(
            self.memory_store.load_by_kind(MemoryKind.DAILY),
            period_start,
            period_end,
        )
        if not in_week:
            logger.info("weekly 跳过:本周无 daily 记忆 %04d-W%02d", iso_year, iso_week)
            return None

        try:
            entry = self.consolidator.compress_to_weekly(
                in_week,
                period_start=period_start,
                period_end=period_end,
            )
        except Exception:
            logger.exception("weekly 压缩失败 %04d-W%02d", iso_year, iso_week)
            return None

        self.memory_store.append(entry)
        logger.info(
            "weekly 压缩完成 %04d-W%02d daily=%d summary_tokens=%d",
            iso_year,
            iso_week,
            len(in_week),
            entry.tokens,
        )
        return entry

    # ------------------------------------------------------------------ monthly

    def rollup_monthly(self, year: int, month: int) -> MemoryEntry | None:
        """把一个月内的 weekly 记忆合并成一条 monthly 记忆。"""
        period_start, period_end = _month_period(year, month)
        if self.memory_store.has_for_period(MemoryKind.MONTHLY, period_start, period_end):
            logger.info("monthly 跳过:已存在 %04d-%02d", year, month)
            return None

        in_month = _select_in_period(
            self.memory_store.load_by_kind(MemoryKind.WEEKLY),
            period_start,
            period_end,
        )
        if not in_month:
            logger.info("monthly 跳过:本月无 weekly 记忆 %04d-%02d", year, month)
            return None

        try:
            entry = self.consolidator.compress_to_monthly(
                in_month,
                period_start=period_start,
                period_end=period_end,
            )
        except Exception:
            logger.exception("monthly 压缩失败 %04d-%02d", year, month)
            return None

        self.memory_store.append(entry)
        logger.info(
            "monthly 压缩完成 %04d-%02d weekly=%d summary_tokens=%d",
            year,
            month,
            len(in_month),
            entry.tokens,
        )
        return entry

    # ----------------------------------------------------------------- 启动时补救

    def catch_up_recent(self, *, now: datetime | None = None) -> dict[str, list[Any]]:
        """启动时跑一次,把最近错过的 daily/weekly/monthly 一并补上。

        策略:
        - daily:当前日期之前(不含今天)所有有事件的日期,缺哪天补哪天。
        - weekly:上一个 ISO 周(若已结束)缺则补。
        - monthly:上一个完整月(若已结束)缺则补。
        """
        now = (now or datetime.now().astimezone())
        today = now.date()
        produced: dict[str, list[Any]] = {"daily": [], "weekly": [], "monthly": []}

        # daily 补救:已有事件文件但还没 daily 记忆的日期。
        for day in self.events_store.list_event_dates():
            if day >= today:
                continue
            entry = self.rollup_daily(day)
            if entry is not None:
                produced["daily"].append(day)

        # weekly 补救:今天所在 ISO 周的"上一周"。
        prev_iso = (now - timedelta(days=now.isoweekday())).isocalendar()
        weekly_entry = self.rollup_weekly(prev_iso.year, prev_iso.week)
        if weekly_entry is not None:
            produced["weekly"].append((prev_iso.year, prev_iso.week))

        # monthly 补救:上一个月。
        if now.month == 1:
            prev_month = (now.year - 1, 12)
        else:
            prev_month = (now.year, now.month - 1)
        monthly_entry = self.rollup_monthly(*prev_month)
        if monthly_entry is not None:
            produced["monthly"].append(prev_month)

        return produced


# ============================================================== 时间区间辅助


def _local_tz():
    """拿当前系统时区,用于构造带 tz 的 period 边界。"""
    return datetime.now().astimezone().tzinfo


def _day_period(target_date: date) -> tuple[datetime, datetime]:
    """返回某日的 [00:00:00, 23:59:59] 本地时区区间。"""
    tz = _local_tz()
    start = datetime.combine(target_date, time.min, tzinfo=tz)
    end = datetime.combine(target_date, time(23, 59, 59), tzinfo=tz)
    return start, end


def _iso_week_period(iso_year: int, iso_week: int) -> tuple[datetime, datetime]:
    """返回某 ISO 周周一 00:00 到周日 23:59:59 的本地时区区间。"""
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    sunday = date.fromisocalendar(iso_year, iso_week, 7)
    tz = _local_tz()
    return (
        datetime.combine(monday, time.min, tzinfo=tz),
        datetime.combine(sunday, time(23, 59, 59), tzinfo=tz),
    )


def _month_period(year: int, month: int) -> tuple[datetime, datetime]:
    """返回某年某月 1 日 00:00 到月末 23:59:59 的本地时区区间。"""
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    tz = _local_tz()
    return (
        datetime.combine(date(year, month, 1), time.min, tzinfo=tz),
        datetime.combine(last_day, time(23, 59, 59), tzinfo=tz),
    )


def _select_in_period(
    entries: list[MemoryEntry],
    period_start: datetime,
    period_end: datetime,
) -> list[MemoryEntry]:
    """从一批记忆里选出 period 完全落在 [period_start, period_end] 内的条目。"""
    return [
        entry
        for entry in entries
        if period_start <= entry.period_start and entry.period_end <= period_end
    ]
