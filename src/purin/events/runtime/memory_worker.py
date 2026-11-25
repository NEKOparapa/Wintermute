"""User-serial background learning from dialogue and historical record imports."""

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from purin.agent import Model
from purin.events.records.preprocess import aggregate, preprocess
from purin.events.runtime.memory_agent import MemoryAgentRunner, MemoryDeferred
from purin.events.storage.profiles import ProfileInput, ProfileStatus
from purin.events.storage.repositories.records import RecordItem
from purin.events.storage.sqlite import SQLiteStore
from purin.memory import MemoryVault

if TYPE_CHECKING:
    from purin.config.models import MemorySettings

logger = logging.getLogger(__name__)


def _pages(text: str, limit: int) -> tuple[str, ...]:
    """Never discard the tail of an oversized user message or imported document."""
    if limit < 1:
        raise ValueError("Memory batch budget cannot hold source metadata")
    return tuple(text[index:index + limit] for index in range(0, len(text), limit)) or ("",)


class MemoryWorker:
    def __init__(
        self,
        store: SQLiteStore,
        vaults: Mapping[str, MemoryVault],
        model: Model,
        settings: "MemorySettings",
        *,
        timezone: str,
    ) -> None:
        self.store = store
        self.vaults = dict(vaults)
        self.model = model
        self.settings = settings
        self.zone = ZoneInfo(timezone)
        self._wakes = {user: asyncio.Event() for user in self.vaults}
        self._locks = {user: asyncio.Lock() for user in self.vaults}
        self._agents = {
            user: MemoryAgentRunner(model, vault, settings) for user, vault in self.vaults.items()
        }

    def wakeup(self) -> None:
        for wake in self._wakes.values():
            wake.set()

    def _available(self, user: str) -> bool:
        return not self.store.memory.profile_status(user).busy

    async def _run(self, user: str, channel: str, text: str, now: datetime) -> None:
        await self._agents[user].run(
            channel, text, today=now.astimezone(self.zone).date(),
            can_continue=lambda: self._available(user),
        )

    def _record_pieces(self, item: RecordItem, owner_id: str | None) -> tuple[str, ...] | None:
        header = json.dumps({
            "item_id": item.item_id,
            "source": item.source_name,
            "record_type": item.record_type,
            "occurred_at": item.occurred_at.isoformat(),
            "title": item.title[:128] if item.title else None,
        }, ensure_ascii=False) + "\n"
        available = self.settings.max_batch_chars - len(header) - 32
        if available < 1:
            raise ValueError("Memory batch budget cannot hold source metadata")
        result = preprocess(
            item, self.store.records.payload(item), max_chars=min(16_000, available),
            previous=self.store.records.previous_payload(item),
        )
        if result.filtered_reason is not None:
            self.store.records.mark(
                item.user_id, [item.item_id], status="filtered", error=result.filtered_reason,
                owner_id=owner_id,
            )
            return None
        return tuple(
            header + f"[资料分片 {index + 1}]\n" + page
            for index, page in enumerate(result.texts)
        )

    async def _records(self, user: str, now: datetime, owner_id: str | None) -> bool:
        pending = self.store.records.pending(user, limit=100)
        if not pending:
            return False
        record_type = pending[0].record_type
        pieces: list[tuple[RecordItem, str]] = []
        remaining: Counter[str] = Counter()
        filtered = False
        # A failed grouped batch is retried one item at a time, isolating poison records.
        retrying = pending[0].attempts > 0
        for item in pending:
            if item.record_type != record_type:
                continue
            if pieces and (retrying or item.attempts > 0):
                break
            try:
                texts = self._record_pieces(item, owner_id)
            except Exception as exc:
                self.store.records.fail(
                    user, [item.item_id], error=type(exc).__name__, owner_id=owner_id
                )
                raise
            if texts is None:
                filtered = True
                continue
            remaining[item.item_id] = len(texts)
            pieces.extend((item, text) for text in texts)
            if retrying or sum(len(text) + 2 for _, text in pieces) >= (
                self.settings.max_batch_chars
            ):
                break

        while pieces:
            batch: list[tuple[RecordItem, str]] = []
            size = 0
            while pieces:
                item, text = pieces[0]
                addition = len(text) + (2 if batch else 0)
                if batch and size + addition > self.settings.max_batch_chars:
                    break
                batch.append(pieces.pop(0))
                size += addition
            ids = tuple(dict.fromkeys(item.item_id for item, _ in batch))
            try:
                self.store.records.start(user, ids, owner_id=owner_id)
                await self._run(
                    user, f"record:{record_type}",
                    aggregate(batch, payload=self.store.records.payload), now,
                )
            except MemoryDeferred:
                raise
            except Exception as exc:
                self.store.records.fail(user, ids, error=type(exc).__name__, owner_id=owner_id)
                raise
            for item, _ in batch:
                remaining[item.item_id] -= 1
            finished = tuple(item_id for item_id in ids if remaining[item_id] == 0)
            if finished:
                self.store.records.mark(user, finished, status="processed", owner_id=owner_id)
            await self.vaults[user].snapshot("后台历史记录整理")
        return filtered or bool(remaining)

    def _dialogue_pages(self, rows: tuple[ProfileInput, ...]) -> tuple[list[str], int]:
        pages: list[str] = []
        size = 0
        through = 0
        for row in rows:
            header = json.dumps({
                "event_id": row.event_id,
                "conversation_id": row.conversation_id,
                "occurred_at": row.occurred_at.isoformat(),
            }, ensure_ascii=False) + "\n用户原话：\n"
            context = (
                "\nprevious_reply（仅供上下文）：\n" + row.previous_reply[:2000]
                if row.previous_reply else ""
            )
            available = self.settings.max_batch_chars - len(header) - len(context) - 32
            if available < 1:
                context = ""
                available = self.settings.max_batch_chars - len(header) - 32
            row_pages = [header + page + context for page in _pages(row.text, available)]
            row_size = sum(len(page) + 2 for page in row_pages)
            if pages and size + row_size > self.settings.max_batch_chars:
                break
            pages.extend(row_pages)
            size += row_size
            through = row.seq
        return pages, through

    async def _dialogue(self, user: str, state: ProfileStatus, owner_id: str | None) -> bool:
        rows = self.store.memory.profile_inputs(user, after=state.cursor, through=state.latest_seq)
        if not rows:
            return False
        pages, through = self._dialogue_pages(rows)
        current: list[str] = []
        size = 0
        for page in pages:
            if current and size + len(page) + 2 > self.settings.max_batch_chars:
                await self._run(user, "conversation", "\n\n".join(current), state.now)
                current, size = [], 0
            current.append(page)
            size += len(page) + (2 if len(current) > 1 else 0)
        if current:
            await self._run(user, "conversation", "\n\n".join(current), state.now)
        advanced = self.store.memory.advance_memory_cursor(
            user, after=state.cursor, through=through, owner_id=owner_id
        )
        if advanced:
            await self.vaults[user].snapshot("后台主会话记忆整理")
        return advanced

    async def process_user(self, user: str, *, owner_id: str | None = None) -> bool:
        async with self._locks[user]:
            try:
                plan = self.store.memory.schedule(user)
                if self.store.now_utc() < plan.next_run_at:
                    return False
                state = self.store.memory.profile_status(user)
                if state.busy:
                    return False
                if await self._records(user, state.now, owner_id):
                    return True
                if state.latest_seq > state.cursor:
                    return await self._dialogue(user, state, owner_id)
                await self._run(user, "maintenance", "执行计划中的记忆整理。", state.now)
                await self.vaults[user].snapshot("定时记忆整理")
                self.store.memory.finish_schedule(user, plan, owner_id)
                return True
            except MemoryDeferred:
                return False

    async def run(self, owner_id: str) -> None:
        # An oversized archive or an unavailable model for one user must not stall others.
        tasks = [
            asyncio.create_task(self._run_user(user, owner_id), name=f"memory-{user}")
            for user in self.vaults
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_user(self, user: str, owner_id: str) -> None:
        failures = 0
        wake = self._wakes[user]
        while True:
            wake.clear()
            try:
                worked = await self.process_user(user, owner_id=owner_id)
            except Exception as exc:
                failures += 1
                delay = min(3600, 5 * 2 ** min(failures - 1, 10))
                logger.warning(
                    "后台记忆整理稍后重试",
                    extra={"user_id": user, "error_type": type(exc).__name__},
                )
                await asyncio.sleep(delay)
            else:
                failures = 0
                if worked:
                    await asyncio.sleep(0)
                    continue
                try:
                    remaining = (
                        self.store.memory.schedule(user).next_run_at - self.store.now_utc()
                    ).total_seconds()
                    delay = self.settings.poll_interval
                    if remaining > 0:
                        delay = min(delay, remaining)
                    await asyncio.wait_for(wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
