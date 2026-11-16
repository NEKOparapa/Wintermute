"""在主会话之外整理用户档案，按稳定的交流游标发布 Markdown。"""

import asyncio
import hashlib
import logging
import os
import re
import stat
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from wintermute.agent import Model
from wintermute.events.runtime.profile import PROFILE_TEMPLATE, fits_profile, read_profile_bytes
from wintermute.events.runtime.profile_summary import (
    ProfileEvidence,
    prune_expired,
    summarize_profile,
)
from wintermute.events.storage.profile_activity import ActivityPattern, update_activity_section
from wintermute.events.storage.profiles import ProfilePublication, ProfileStatus
from wintermute.events.storage.sqlite import SQLiteStore

if TYPE_CHECKING:
    from wintermute.config.models import ProfileDreamingSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileVersion:
    raw: bytes | None

    @property
    def digest(self) -> str:
        value = b"absent" if self.raw is None else b"file\0" + self.raw
        return hashlib.sha256(value).hexdigest()

    @property
    def markdown(self) -> str:
        if self.raw is None:
            return PROFILE_TEMPLATE
        return self.raw.decode("utf-8-sig").replace("\r\n", "\n")

    def encode(self, markdown: str) -> str:
        if self.raw and b"\r\n" in self.raw:
            markdown = markdown.replace("\n", "\r\n")
        if self.raw and self.raw.startswith(b"\xef\xbb\xbf"):
            markdown = "\ufeff" + markdown
        return markdown


def _sections(markdown: str) -> list[tuple[str, str]]:
    return [
        (part.partition("\n")[0].strip() if part.startswith("## ") else "", part)
        for part in re.split(r"(?m)(?=^## )", markdown)
        if part
    ]


def _keep_manual_edits(base: str, edited: str, proposed: str) -> str:
    """Keep a concurrently edited section in full; apply changes to untouched sections."""
    original, current, changes = map(_sections, (base, edited, proposed))
    # A freeform rewrite or ambiguous duplicate heading takes precedence over the proposal.
    if any(len(dict(parts)) != len(parts) for parts in (original, current, changes)):
        return edited
    old, updated, new = map(dict, (original, current, changes))
    if old.get("") != updated.get(""):
        return edited
    chunks = [new.get(key, "") if old.get(key) == value else value for key, value in current]
    for key, value in changes:
        if key not in old and key not in updated:
            chunks.extend(("\n" if chunks else "", value))
    merged = "".join(chunks)
    return merged if fits_profile(merged) else edited


def _write_staged(fd: int, content: bytes) -> None:
    with os.fdopen(fd, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _replace_if_current(path: Path, staged: Path, expected: str) -> bool:
    """A last-minute manual edit takes precedence over this batch of suggestions."""
    if FileVersion(read_profile_bytes(path)).digest != expected:
        return True
    if path.exists():
        staged.chmod(stat.S_IMODE(path.stat().st_mode))
    os.replace(staged, path)
    if os.name == "posix":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return True


class ProfileWorker:
    def __init__(
        self,
        store: SQLiteStore,
        files: Mapping[str, Path],
        model: Model,
        settings: "ProfileDreamingSettings",
        *,
        timezone: str,
    ) -> None:
        self.store = store
        self.files = dict(files)
        self.model = model
        self.settings = settings
        self.zone = ZoneInfo(timezone)
        self._patterns: dict[str, tuple[datetime, ActivityPattern]] = {}

    def _due(self, state: ProfileStatus, pattern: ActivityPattern) -> bool:
        if state.oldest_pending_at is None or state.latest_received_at is None:
            return False
        if (state.now - state.oldest_pending_at).total_seconds() >= self.settings.max_wait_seconds:
            return True
        quiet = (state.now - state.latest_received_at).total_seconds()
        if pattern.low_activity is None:
            return quiet >= self.settings.cold_start_idle_seconds
        return pattern.low_activity and quiet >= self.settings.idle_seconds

    async def _settle(
        self, user: str, path: Path, pending: ProfilePublication, owner_id: str | None
    ) -> bool:
        if str(path) != pending.path:
            return self.store.settle_profile(user, pending, advance=False, owner_id=owner_id)
        target = pending.content.encode("utf-8")
        current = FileVersion(await asyncio.to_thread(read_profile_bytes, path))
        if current.raw == target or current.digest != pending.base_digest:
            # A rename survived a crash, or the user edited the file afterwards.
            return self.store.settle_profile(user, pending, advance=True, owner_id=owner_id)
        if current.raw is None and not target:
            return self.store.settle_profile(user, pending, advance=True, owner_id=owner_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".wintermute-")
        staged = Path(name)
        writing = asyncio.create_task(asyncio.to_thread(_write_staged, fd, target))
        try:
            await asyncio.shield(writing)
            return self.store.settle_profile(
                user,
                pending,
                advance=True,
                owner_id=owner_id,
                publish=lambda: _replace_if_current(path, staged, pending.base_digest),
            )
        finally:
            try:
                await writing
            finally:
                await asyncio.to_thread(staged.unlink, missing_ok=True)

    async def process_user(self, user: str, *, owner_id: str | None = None) -> bool:
        state = self.store.profile_status(user)
        path = self.files[user]
        if state.pending is not None:
            return await self._settle(user, path, state.pending, owner_id)
        if state.busy:
            return False
        local = state.now.astimezone(self.zone)
        slot = local.replace(minute=local.minute // 30 * 30, second=0, microsecond=0)
        cached = self._patterns.get(user)
        refreshed = cached is None or cached[0] != slot
        if cached is None or cached[0] != slot:
            pattern = await self.store.read_profile_activity(user, state.now, self.zone.key)
        else:
            pattern = cached[1]
        due = self._due(state, pattern)
        if not due and not refreshed:
            return False
        base = FileVersion(await asyncio.to_thread(read_profile_bytes, path))
        markdown = prune_expired(base.markdown, today=local.date())
        through = state.cursor
        if due:
            evidence: list[ProfileEvidence] = []
            size = 0
            for row in self.store.profile_inputs(
                user, after=state.cursor, through=state.latest_seq
            ):
                if evidence and size + len(row.text) > 64 * 1024:
                    break
                evidence.append(
                    ProfileEvidence(
                        row.event_id,
                        row.occurred_at.astimezone(self.zone),
                        row.text,
                        previous_reply=row.previous_reply,
                        conversation_id=row.conversation_id,
                    )
                )
                size += len(row.text)
                through = row.seq
            if evidence:
                # The 28-day device scan and file read may have yielded to a new conversation.
                latest = self.store.profile_status(user)
                if (
                    latest.busy
                    or latest.latest_seq != state.latest_seq
                    or latest.cursor != state.cursor
                ):
                    return False
                markdown = await summarize_profile(
                    self.model,
                    markdown,
                    evidence,
                    today=local.date(),
                    timeout=self.settings.model_timeout,
                )
        with_activity = update_activity_section(markdown, pattern)
        if fits_profile(base.encode(with_activity)):
            markdown = with_activity
        current = FileVersion(await asyncio.to_thread(read_profile_bytes, path))
        if current.raw != base.raw:
            if current.raw is None or base.raw is None:
                markdown = current.markdown
            else:
                markdown = _keep_manual_edits(base.markdown, current.markdown, markdown)
        if not fits_profile(current.encode(markdown)):
            markdown = current.markdown
        if current.markdown == markdown and through == state.cursor:
            self._patterns[user] = (slot, pattern)
            return False
        if current.raw is None and markdown == PROFILE_TEMPLATE:
            content = ""
        else:
            content = current.encode(markdown)
        publication = ProfilePublication(
            state.cursor, state.latest_seq, through, str(path), current.digest, content
        )
        if not self.store.stage_profile(
            user,
            cursor=state.cursor,
            latest_seq=state.latest_seq,
            through=through,
            path=str(path),
            base_digest=current.digest,
            content=content,
            owner_id=owner_id,
        ):
            return False
        settled = await self._settle(user, path, publication, owner_id)
        if settled:
            self._patterns[user] = (slot, pattern)
        return settled

    async def run(self, owner_id: str) -> None:
        failures: Counter[str] = Counter()
        retry_at: dict[str, float] = {}
        loop = asyncio.get_running_loop()
        while True:
            for user in self.files:
                if loop.time() < retry_at.get(user, 0):
                    continue
                try:
                    await self.process_user(user, owner_id=owner_id)
                except Exception as exc:
                    failures[user] += 1
                    delay = min(3600, 5 * 2 ** min(failures[user], 10))
                    retry_at[user] = loop.time() + delay
                    logger.warning("用户档案整理稍后重试", extra={"error_type": type(exc).__name__})
                else:
                    failures.pop(user, None)
                    retry_at.pop(user, None)
            await asyncio.sleep(self.settings.poll_interval)
