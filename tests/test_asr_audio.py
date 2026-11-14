"""Exercise exact Ogg/Opus boundaries using a real encoder with nonzero pre-skip."""

import io
import math
import wave
from array import array
from fractions import Fraction
from pathlib import Path

import av
import pytest

from wintermute.events.slices.asr_audio import prepare_audio

RATE = 16000


def write_opus(path: Path, samples: int, *, pause_seconds: tuple[int, int] | None = None) -> None:
    # Each two-minute section has a distinct tone to expose dropped or reordered audio.
    tones = [
        array("h", (int(8000 * math.sin(2 * math.pi * hz * t / RATE)) for t in range(RATE)))
        for hz in (220, 440, 880)
    ]
    with av.open(path, mode="w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=RATE)
        stream.layout = "mono"
        for offset in range(0, samples, RATE):
            count = min(samples - offset, RATE)
            frame = av.AudioFrame(format="s16", layout="mono", samples=count)
            frame.pts = offset
            frame.time_base = Fraction(1, RATE)
            frame.sample_rate = RATE
            second = offset // RATE
            if pause_seconds and pause_seconds[0] <= second < pause_seconds[1]:
                frame.planes[0].update(bytes(2 * count))
            else:
                frame.planes[0].update(tones[offset // (120 * RATE)][:count].tobytes())
            container.mux(stream.encode(frame))
        container.mux(stream.encode(None))


def decoded_pcm(path: Path) -> bytes:
    with av.open(path) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        pcm = bytearray()
        for frame in container.decode(audio=0):
            for output in resampler.resample(frame):
                pcm.extend(bytes(output.planes[0])[: 2 * output.samples])
        for output in resampler.resample(None):
            pcm.extend(bytes(output.planes[0])[: 2 * output.samples])
    return bytes(pcm)


def test_exactly_two_minutes_passes_through(tmp_path: Path) -> None:
    recording = tmp_path / "short.ogg"
    write_opus(recording, 120 * RATE)
    assert prepare_audio(recording, recording.stat().st_size) == [
        ("audio/ogg", recording.read_bytes())
    ]
    with pytest.raises(ValueError, match="size"):
        prepare_audio(recording, recording.stat().st_size - 1)


@pytest.mark.parametrize(
    ("pause", "expected_cut"),
    [
        ((118, 119), None),
        ((100, 101), 120 * RATE),
        ((0, 124), 120 * RATE),
    ],
    ids=["pause-near-limit", "only-distant-pause", "all-silence"],
)
def test_pause_selection_preserves_every_sample(
    tmp_path: Path, pause: tuple[int, int], expected_cut: int | None
) -> None:
    recording = tmp_path / "pause-before-deadline.ogg"
    write_opus(recording, 123 * RATE + 1, pause_seconds=pause)
    expected_pcm = decoded_pcm(recording)

    parts = prepare_audio(recording, recording.stat().st_size)
    pcm = bytearray()
    cut_samples = []
    for mime, data in parts:
        assert mime == "audio/wav"
        with wave.open(io.BytesIO(data)) as wav:
            count = wav.getnframes()
            assert 0 < count <= 120 * RATE
            pcm.extend(wav.readframes(count))
            cut_samples.append(count)
    assert len(cut_samples) == 2
    if expected_cut is None:
        assert pause[0] * RATE < cut_samples[0] < pause[1] * RATE
    else:
        assert cut_samples[0] == expected_cut
    assert len(pcm) == (123 * RATE + 1) * 2
    assert pcm == expected_pcm


def test_byte_limit_independently_triggers_splitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = tmp_path / "large-for-duration.ogg"
    write_opus(recording, 2 * RATE)
    # Scale down the wire limit and use an odd number to exercise full 16-bit samples.
    limit = 4097
    monkeypatch.setattr("wintermute.events.slices.asr_audio._MAX_UPLOAD_BYTES", limit)
    assert recording.stat().st_size > limit
    parts = prepare_audio(recording, recording.stat().st_size)
    samples = 0
    for mime, data in parts:
        assert mime == "audio/wav" and len(data) <= limit
        with wave.open(io.BytesIO(data)) as wav:
            samples += wav.getnframes()
    assert samples == 2 * RATE


@pytest.mark.parametrize("frames", [[120 * RATE, 1], [120 * RATE, 120 * RATE, 60 * RATE]])
def test_oversized_duration_preserves_samples_and_order(tmp_path: Path, frames: list[int]) -> None:
    recording = tmp_path / "long.ogg"
    write_opus(recording, sum(frames))
    parts = prepare_audio(recording, recording.stat().st_size)
    frequencies = (220, 440, 880)[: len(frames)]
    for (mime, data), expected, hz in zip(parts, frames, frequencies, strict=True):
        assert mime == "audio/wav" and len(data) <= 8 * 1024 * 1024
        with wave.open(io.BytesIO(data)) as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, RATE)
            assert wav.getnframes() == expected
            if expected > RATE:
                wav.setpos(RATE)
                audio = array("h", wav.readframes(RATE))
                crossings = sum(a <= 0 < b for a, b in zip(audio, audio[1:], strict=False))
                assert abs(crossings - hz) <= 1
