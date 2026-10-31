"""音频转录与完整覆盖的分级摘要；音频和转录均保留供追溯。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from wintermute.events.core.types import AudioSliceData, EventRecord
from wintermute.events.slices.compression import (
    SliceCompressionError,
    SliceCompressor,
    bounded_text,
    serialize_slices,
)
from wintermute.events.storage.sqlite import SQLiteStore

if TYPE_CHECKING:
    from wintermute.events.slices.asr import AudioTranscriber


class AudioSliceProcessor:
    """每段先持久化 ASR 转录，再独立总结，绝不静默裁掉长转录尾部。"""

    strategy = "audio_asr_model"

    def __init__(
        self,
        store: SQLiteStore,
        transcriber: AudioTranscriber,
        compressor: SliceCompressor,
        *,
        storage_path: str | Path,
        max_segment_bytes: int = 4 * 1024 * 1024,
        max_input_chars: int = 12000,
        max_output_chars: int = 1200,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        self.store = store
        self.transcriber = transcriber
        self.compressor = compressor
        self.storage_path = Path(storage_path).resolve()
        self.max_segment_bytes = max_segment_bytes
        self.max_input_chars = max_input_chars
        self.max_output_chars = max_output_chars
        self._zone = ZoneInfo(timezone)

    def _verified_path(self, audio: AudioSliceData) -> Path:
        path = (self.storage_path / audio.file).resolve()
        if path.parent != self.storage_path or not path.is_file():
            raise SliceCompressionError("Audio file is outside configured storage or missing")
        if path.stat().st_size != audio.bytes or audio.bytes > self.max_segment_bytes:
            raise SliceCompressionError("Audio file size differs from its persisted reference")
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != audio.sha256:
            raise SliceCompressionError("Audio file digest differs from its persisted reference")
        return path

    def _text_event(self, event: EventRecord, text: str) -> EventRecord:
        return event.model_copy(update={"payload": {"text": text}}, deep=True)

    def _split(self, event: EventRecord, text: str) -> list[EventRecord]:
        """按真实 JSON 大小分块；每个输入字符都进入某个模型请求。"""
        parts: list[EventRecord] = []
        offset = 0
        while offset < len(text):
            low, high = 0, min(len(text) - offset, self.max_input_chars)
            while low < high:
                middle = (low + high + 1) // 2
                candidate = self._text_event(event, text[offset : offset + middle])
                if len(serialize_slices((candidate,), timezone=self._zone)) <= self.max_input_chars:
                    low = middle
                else:
                    high = middle - 1
            if not low:
                raise SliceCompressionError("Audio slice metadata exceeds summary input budget")
            parts.append(self._text_event(event, text[offset : offset + low]))
            offset += low
        return parts

    async def process(self, event: EventRecord, *, owner_id: str | None = None) -> str:
        audio = AudioSliceData.model_validate(event.payload["audio"])
        transcript = self.store.get_audio_transcript(event.event_id)
        if transcript is None:
            path = await asyncio.to_thread(self._verified_path, audio)
            transcript = await self.transcriber.transcribe(path)
            self.store.save_audio_transcript(event.event_id, transcript, owner_id=owner_id)
        if not transcript.strip():
            return "该录音片段未识别到语音。"
        description = (
            f"录音 {audio.recording_id}，第 {audio.sequence} 段，"
            f"本次录音起点后 {audio.start_sample / audio.sample_rate:g} 秒开始，"
            f"时长 {audio.samples / audio.sample_rate:g} 秒。\n"
        )
        parts = self._split(event, description + transcript)
        # 多块时每块输出至多输入预算的 1/4，确保后续汇总严格收敛。
        budget = min(self.max_output_chars, max(32, self.max_input_chars // 4))
        for _ in range(32):
            if len(parts) == 1:
                result = await self.compressor.compress(
                    (parts[0],), max_chars=self.max_output_chars
                )
                if not isinstance(result, str) or not result.strip():
                    raise SliceCompressionError("Audio summary model returned empty text")
                return bounded_text(result.strip(), self.max_output_chars)
            summaries: list[str] = []
            for part in parts:
                result = await self.compressor.compress((part,), max_chars=budget)
                if not isinstance(result, str) or not result.strip():
                    raise SliceCompressionError("Audio summary model returned empty text")
                summaries.append(bounded_text(result.strip(), budget))
            parts = self._split(event, "\n".join(summaries))
        raise SliceCompressionError("Audio summary did not converge within the reduction limit")
