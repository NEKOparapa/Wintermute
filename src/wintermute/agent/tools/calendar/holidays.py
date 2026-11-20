"""Load and cache holiday-cn bulletins by their publication year."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date
from time import time
from typing import Literal

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar
from pydantic import BaseModel, Field

from wintermute.events.storage.repositories.holidays import HolidayYearStore

_SOURCES = (
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master",
    "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master",
)
_DAY = 24 * 60 * 60


class Holiday(BaseModel):
    name: str = Field(min_length=1)
    date: date
    isOffDay: bool


class _Bulletin(BaseModel):
    year: int
    papers: list[str] = Field(min_length=1)
    days: list[Holiday] = Field(min_length=1)


@dataclass(frozen=True)
class YearData:
    days: dict[date, Holiday]
    papers: tuple[str, ...]
    status: Literal["published", "stale_cache", "no_data", "unavailable"]


def _parse(year: int, raw: bytes) -> YearData:
    bulletin = _Bulletin.model_validate_json(raw, strict=True)
    if bulletin.year != year:
        raise ValueError("holiday-cn 年度数据与所请求年份不符")
    return YearData({d.date: d for d in bulletin.days}, tuple(bulletin.papers), "published")


async def download_year(year: int) -> bytes | None:
    async with ClientSession(
        timeout=ClientTimeout(total=8), trust_env=True, cookie_jar=DummyCookieJar()
    ) as session:
        for source in _SOURCES:
            try:
                async with session.get(f"{source}/{year}.json") as response:
                    if response.status == 404:
                        return None
                    response.raise_for_status()
                    return await response.read()
            except (TimeoutError, ClientError):
                continue
    raise OSError("无法取得 holiday-cn 年度数据")


class HolidayCalendar:
    def __init__(
        self, store: HolidayYearStore, fetch: Callable[[int], Awaitable[bytes | None]] | None = None
    ) -> None:
        self._store = store
        self._fetch = fetch or download_year
        self._years: dict[int, tuple[float, YearData]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def year(self, year: int) -> YearData:
        async with self._locks.setdefault(year, asyncio.Lock()):
            now = time()
            recent = self._years.get(year)
            if recent is not None and recent[0] > now:
                return recent[1]
            saved = self._store.get(year)
            previous = None
            if saved is not None:
                try:
                    previous = _parse(year, saved[1])
                except ValueError:
                    pass  # Replace an invalid cache entry only after validating a fresh response.
                if previous is not None and saved[0] + _DAY > now:
                    self._years[year] = (saved[0] + _DAY, previous)
                    return previous
            try:
                raw = await self._fetch(year)
                if raw is not None:
                    data = _parse(year, raw)
                    self._store.save(year, now, raw)
                    self._years[year] = (now + _DAY, data)
                    return data
                data = YearData({}, (), "no_data")
                retry_in = 3600
            except (ValueError, OSError):
                data = YearData({}, (), "unavailable")
                retry_in = 300
            if previous is not None:
                data = replace(previous, status="stale_cache")
            self._years[year] = (now + retry_in, data)
            return data
