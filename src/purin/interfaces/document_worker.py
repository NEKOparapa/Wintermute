"""Convert uploaded bytes in a short-lived worker. stdout contains only the resulting Markdown."""

import asyncio
import io
import json
import sys
import zipfile
from itertools import chain
from typing import Any, BinaryIO

import av
from markitdown import (
    PRIORITY_SPECIFIC_FILE_FORMAT,
    DocumentConverter,
    DocumentConverterResult,
    MarkItDown,
    StreamInfo,
    UnsupportedFormatException,
)
from markitdown.converters import AudioConverter

from purin.events.slices.asr import DeepSeekASR
from purin.events.slices.asr_audio import pcm_wav_parts


def check_archive(data: bytes) -> None:
    # Office, EPUB and ZIP converters may recursively inflate embedded files.
    remaining = 128 * 1024 * 1024
    entries = 4096

    def visit(stream: io.BytesIO, depth: int) -> None:
        nonlocal remaining, entries
        if not zipfile.is_zipfile(stream):
            return
        if depth > 6:
            raise ValueError("Archive nesting limit")
        with zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            entries -= len(members)
            remaining -= sum(item.file_size for item in members)
            if remaining < 0 or entries < 0:
                raise ValueError("Archive expansion limit")
            for item in members:
                with archive.open(item) as member:
                    head = member.read(4)
                    if head == b"PK\x03\x04":
                        visit(io.BytesIO(head + member.read()), depth + 1)

    visit(io.BytesIO(data), 0)


class ChatAudio(AudioConverter):
    """Use the same configured speech service as chat dictation, also inside ZIP files."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.failure = 0

    def convert(
        self, file_stream: BinaryIO, stream_info: StreamInfo, **kwargs: Any
    ) -> DocumentConverterResult:
        if not self.settings["api_key"]:
            self.failure = 4
            raise ValueError("Missing ASR key")
        try:
            with av.open(file_stream, mode="r", options={"protocol_whitelist": "pipe"}) as media:
                if not media.streams.audio:
                    return DocumentConverterResult(markdown="（文件没有音轨。）")
                resample = av.AudioResampler(format="s16", layout="mono", rate=16000)
                pcm = bytearray()
                for frame in chain(media.decode(media.streams.audio[0]), [None]):
                    for chunk in resample.resample(frame):
                        pcm.extend(memoryview(chunk.planes[0])[: chunk.samples * 2])
                        if len(pcm) > 32000 * 3600:
                            raise ValueError("Audio exceeds one hour")
            transcript = asyncio.run(
                DeepSeekASR(**self.settings).transcribe_parts(pcm_wav_parts(pcm))
            )
            return DocumentConverterResult(markdown=transcript or "（未识别到语音。）")
        except Exception:
            self.failure = 5
            raise


class ChatMarkItDown(MarkItDown):
    def __init__(self, audio: ChatAudio) -> None:
        self.audio = audio
        super().__init__(enable_plugins=False)

    def register_converter(
        self, converter: DocumentConverter, *, priority: float = PRIORITY_SPECIFIC_FILE_FORMAT
    ) -> None:
        super().register_converter(
            self.audio if isinstance(converter, AudioConverter) else converter, priority=priority
        )


def main() -> int:
    header = json.loads(sys.stdin.buffer.readline())
    data = sys.stdin.buffer.read()
    audio = ChatAudio(header["asr"])
    try:
        check_archive(data)
        parser = ChatMarkItDown(audio)
        try:
            result = parser.convert_stream(
                io.BytesIO(data), stream_info=StreamInfo(extension=header["extension"] or None)
            )
        except UnsupportedFormatException:
            # Content detection can return "unknown" for very short extensionless text.
            if header["extension"]:
                return 2
            try:
                plain = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                return 2
            if any(not c.isprintable() and not c.isspace() for c in plain):
                return 2
            result = parser.convert_stream(
                io.BytesIO(data), stream_info=StreamInfo(mimetype="text/plain", charset="utf-8-sig")
            )
        if audio.failure:
            return audio.failure
        markdown = result.markdown.replace("\x00", "").encode("utf-8")
        sys.stdout.buffer.write(markdown)
        return 0
    except UnsupportedFormatException:
        return 2
    except Exception:
        return audio.failure or 1


if __name__ == "__main__":
    raise SystemExit(main())
