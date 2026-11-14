"""Prepare a stored Wintermute Ogg/Opus segment for the DeepSeek file API."""

import io
import wave
from collections.abc import Iterator
from itertools import chain
from pathlib import Path

_MAX_UPLOAD_BYTES = 8 * 1024 * 1024
_MAX_UPLOAD_SECONDS = 120
_SAMPLE_RATE = 16000
_FRAME_SAMPLES = _SAMPLE_RATE * 20 // 1000
_MIN_PAUSE_SAMPLES = _SAMPLE_RATE * 300 // 1000
_LOOKBACK_SAMPLES = _SAMPLE_RATE * 15


def _speech_pauses(pcm: memoryview) -> Iterator[tuple[int, int]]:
    """Yield sustained non-speech intervals in 16 kHz PCM sample positions."""
    import webrtcvad  # type: ignore[import-untyped]

    vad = webrtcvad.Vad(2)
    sample_count = len(pcm) // 2
    complete_samples = sample_count - sample_count % _FRAME_SAMPLES
    pause_start = 0
    for start in range(0, complete_samples, _FRAME_SAMPLES):
        end = start + _FRAME_SAMPLES
        if vad.is_speech(pcm[start * 2 : end * 2], _SAMPLE_RATE):
            if start - pause_start >= _MIN_PAUSE_SAMPLES:
                yield pause_start, start
            pause_start = end
    if complete_samples - pause_start >= _MIN_PAUSE_SAMPLES:
        yield pause_start, complete_samples


def _part_ends(pcm: memoryview, max_samples: int) -> Iterator[int]:
    """Choose a recent pause at each upload limit, keeping the entire recording."""
    samples = len(pcm) // 2
    if samples > max_samples:
        pauses = list(_speech_pauses(pcm))
        lookback = min(_LOOKBACK_SAMPLES, max_samples // 2)
        start = 0
        while samples - start > max_samples:
            limit = start + max_samples
            cut = limit
            for left, right in reversed(pauses):
                if right < limit - lookback or left <= limit <= right:
                    break
                midpoint = (left + right) // 2
                if right < limit and midpoint >= limit - lookback:
                    cut = midpoint
                    break
            yield cut
            start = cut
    yield samples


def prepare_audio(path: Path, max_audio_bytes: int) -> list[tuple[str, bytes]]:
    """Pass short recordings through, or decode once and split near speech pauses."""
    import av

    with path.open("rb") as source:
        data = source.read(max_audio_bytes + 1)
    if not data or len(data) > max_audio_bytes:
        raise ValueError("Invalid recording size")

    try:
        with av.open(io.BytesIO(data), mode="r", format="ogg") as container:
            if len(container.streams.audio) != 1:
                raise ValueError("Expected a single Opus stream")
            stream = container.streams.audio[0]
            head = stream.codec_context.extradata or b""
            if len(head) < 19 or not head.startswith(b"OpusHead\x01\x01"):
                raise ValueError("Expected a mono Opus stream")
            if stream.duration is None or stream.time_base is None:
                raise ValueError("Missing Opus duration")
            # FFmpeg includes the Opus encoder's pre-skip in the final 48 kHz granule.
            granules = stream.duration * stream.time_base * 48000
            samples = (granules - int.from_bytes(head[10:12], "little")) / 3
            if samples.denominator != 1 or not 0 < samples <= 300 * _SAMPLE_RATE:
                raise ValueError("Invalid recording duration")
            if samples <= _MAX_UPLOAD_SECONDS * _SAMPLE_RATE and len(data) <= _MAX_UPLOAD_BYTES:
                return [("audio/ogg", data)]

            expected_bytes = int(samples) * 2
            pcm = bytearray()
            resampler = av.AudioResampler(format="s16", layout="mono", rate=_SAMPLE_RATE)
            # Flush the resampler once at the end, preserving its delay across boundaries.
            for frame in chain(container.decode(stream), [None]):
                for output in resampler.resample(frame):
                    if len(pcm) + output.samples * 2 > expected_bytes:
                        raise ValueError("Decoded audio exceeds declared duration")
                    pcm.extend(memoryview(output.planes[0])[: output.samples * 2])
            if len(pcm) != expected_bytes:
                raise ValueError("Incomplete decoded recording")
    except av.FFmpegError:
        raise ValueError("Invalid Ogg/Opus recording") from None

    parts: list[tuple[str, bytes]] = []
    # Mono PCM WAV uses a 44-byte header and two bytes per complete sample.
    part_samples = min(_MAX_UPLOAD_SECONDS * _SAMPLE_RATE, (_MAX_UPLOAD_BYTES - 44) // 2)
    audio = memoryview(pcm)
    start = 0
    for end in _part_ends(audio, part_samples):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setparams((1, 2, _SAMPLE_RATE, 0, "NONE", "not compressed"))
            wav.writeframes(audio[start * 2 : end * 2])
        parts.append(("audio/wav", buffer.getvalue()))
        start = end
    return parts
