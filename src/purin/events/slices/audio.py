"""保存音频转写并按录音发生时间送入合并线。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from purin.events.core.types import AudioSliceData, EventRecord
from purin.events.slices.errors import SliceProcessingError
from purin.events.storage.sqlite import SQLiteStore

if TYPE_CHECKING:
    from purin.events.slices.asr import AudioTranscriber


class AudioSliceProcessor:
    """每段持久化并返回完整的 ASR 转写。"""

    strategy = "audio_asr"

    def __init__(
        self,
        store: SQLiteStore,
        transcriber: AudioTranscriber,
        *,
        storage_path: str | Path,
        max_segment_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.store = store
        self.transcriber = transcriber
        self.storage_path = Path(storage_path).resolve()
        self.max_segment_bytes = max_segment_bytes

    def _verified_path(self, audio: AudioSliceData) -> Path:
        path = (self.storage_path / audio.file).resolve()
        if path.parent != self.storage_path or not path.is_file():
            raise SliceProcessingError("Audio file is outside configured storage or missing")
        if path.stat().st_size != audio.bytes or audio.bytes > self.max_segment_bytes:
            raise SliceProcessingError("Audio file size differs from its persisted reference")
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != audio.sha256:
            raise SliceProcessingError("Audio file digest differs from its persisted reference")
        return path

    async def process(self, event: EventRecord, *, owner_id: str | None = None) -> str:
        audio = AudioSliceData.model_validate(event.payload["audio"])
        transcript = self.store.slices.get_audio_transcript(event.event_id)
        if transcript is None:
            path = await asyncio.to_thread(self._verified_path, audio)
            transcript = await self.transcriber.transcribe(path)
            self.store.slices.save_audio_transcript(event.event_id, transcript, owner_id=owner_id)
        if not transcript.strip():
            return "该录音片段未识别到语音。"
        return transcript
