"""User-serial background learning from dialogue and historical record imports."""

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from wintermute.agent import Model
from wintermute.events.records.preprocess import aggregate, preprocess
from wintermute.events.runtime.memory_agent import MemoryAgentRunner, MemoryDeferred
from wintermute.events.storage.profile_activity import ActivityPattern
from wintermute.events.storage.profiles import ProfileInput, ProfileStatus
from wintermute.events.storage.records import RecordItem
from wintermute.events.storage.sqlite import SQLiteStore
from wintermute.memory import MemoryVault, build_memory_prompt

if TYPE_CHECKING:
    from wintermute.config.models import MemorySettings

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
        self._patterns: dict[str, tuple[datetime, ActivityPattern]] = {}
        self._compaction: set[str] = set()
        self._agents = {
            user: MemoryAgentRunner(model, vault, settings) for user, vault in self.vaults.items()
        }

    def wakeup(self) -> None:
        for wake in self._wakes.values():
            wake.set()

    def request_compaction(self, user: str) -> None:
        if user in self.vaults:
            self._compaction.add(user)
            self.wakeup()

    def _available(self, user: str) -> bool:
        return not self.store.profile_status(user).busy

    def _dialogue_due(self, state: ProfileStatus, pattern: ActivityPattern) -> bool:
        if state.oldest_pending_at is None or state.latest_received_at is None:
            return False
        if (state.now - state.oldest_pending_at).total_seconds() >= self.settings.max_wait_seconds:
            return True
        quiet = (state.now - state.latest_received_at).total_seconds()
        if pattern.low_activity is None:
            return quiet >= self.settings.cold_start_idle_seconds
        return pattern.low_activity and quiet >= self.settings.idle_seconds

    async def _activity(self, user: str, now: datetime) -> ActivityPattern:
        local = now.astimezone(self.zone)
        slot = local.replace(minute=local.minute // 30 * 30, second=0, microsecond=0)
        cached = self._patterns.get(user)
        if cached is not None and cached[0] == slot:
            return cached[1]
        pattern = await self.store.read_profile_activity(user, now, self.zone.key)
        if not self._available(user):
            raise MemoryDeferred("Foreground conversation started during activity scan")
        await self.vaults[user].update_activity(pattern.markdown() + "\n")
        self._patterns[user] = (slot, pattern)
        return pattern

    async def _run(self, user: str, channel: str, text: str, now: datetime) -> None:
        await self._agents[user].run(
            channel, text, today=now.astimezone(self.zone).date(),
            can_continue=lambda: self._available(user),
        )

    def _records_due(self, user: str, now: datetime) -> bool:
        times = self.store.records.queue_times(user)
        if times is None:
            return False
        oldest, latest = times
        return (
            (now - latest).total_seconds() >= self.settings.debounce_seconds
            or (now - oldest).total_seconds() >= self.settings.record_max_wait_seconds
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
        rows = self.store.profile_inputs(user, after=state.cursor, through=state.latest_seq)
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
        advanced = self.store.advance_memory_cursor(
            user, after=state.cursor, through=through, owner_id=owner_id
        )
        if advanced:
            await self.vaults[user].snapshot("后台主会话记忆整理")
        return advanced

    async def _maintenance(
        self, user: str, state: ProfileStatus, pattern: ActivityPattern, owner_id: str | None
    ) -> bool:
        today = state.now.astimezone(self.zone).date()
        if self.store.housekeeping_date(user) == today:
            self._compaction.discard(user)
            return False
        prompt = await asyncio.to_thread(
            build_memory_prompt, self.vaults[user], self.settings.prompt_budgets, today=today
        )
        compaction = user in self._compaction or prompt.needs_compaction
        last_input_at = state.latest_received_at
        if last_input_at is None:
            batches, _ = self.store.records.list(user, limit=1)
            if batches:
                last_input_at = batches[0].created_at
        quiet = (
            last_input_at is not None
            and (state.now - last_input_at).total_seconds()
            >= self.settings.cold_start_idle_seconds
        )
        if not compaction and not (pattern.low_activity is True or (
            pattern.low_activity is None and quiet
        )):
            return False
        await self._run(
            user, "maintenance",
            "执行每日记忆维护。" + (
                "常驻记忆接近预算，优先精简并下沉长尾内容。" if compaction else ""
            ),
            state.now,
        )
        self.store.set_housekeeping_date(user, today, owner_id=owner_id)
        self._compaction.discard(user)
        await self.vaults[user].snapshot("每日记忆维护")
        return True

    async def process_user(self, user: str, *, owner_id: str | None = None) -> bool:
        async with self._locks[user]:
            try:
                state = self.store.profile_status(user)
                if state.busy:
                    return False
                pattern = await self._activity(user, state.now)
                if self._records_due(user, state.now):
                    return await self._records(user, state.now, owner_id)
                if self._dialogue_due(state, pattern):
                    return await self._dialogue(user, state, owner_id)
                return await self._maintenance(user, state, pattern, owner_id)
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
                    await asyncio.wait_for(wake.wait(), timeout=self.settings.poll_interval)
                except TimeoutError:
                    pass
