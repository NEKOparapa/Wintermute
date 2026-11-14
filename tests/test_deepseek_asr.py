"""Exercise the DeepSeek wire protocol and device-to-SQLite transcription over loopback HTTP."""

import asyncio
import io
import wave
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from test_asr_audio import write_opus

from wintermute.agent import AgentContext, AssistantMessage, ModelEvent
from wintermute.config import AppConfig, DeepSeekASRSettings, factory
from wintermute.events import SQLiteStore
from wintermute.events.slices.asr import AudioTranscriptionError, DeepSeekASR
from wintermute.interfaces.service import ConversationService


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "private-meeting-name.ogg"
    write_opus(path, 320)
    return path


@asynccontextmanager
async def asr_server(
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> AsyncIterator[str]:
    app = web.Application(client_max_size=9 * 1024 * 1024)
    app.router.add_post("/v1/tools/asr", handler)
    async with TestServer(app, shutdown_timeout=0.1) as server:
        yield str(server.make_url("/v1/tools/asr"))


async def read_upload(request: web.Request, *, mime: str) -> bytes:
    assert request.headers["Authorization"] == "Bearer voice-secret"
    assert request.content_type == "multipart/form-data"
    assert not any(name.lower().startswith("x-api-") for name in request.headers)
    parts = await request.multipart()
    file = await parts.next()
    assert file is not None and file.name == "file" and file.filename
    assert file.headers["Content-Type"] == mime
    data = await file.read()
    assert await parts.next() is None
    return bytes(data)


@pytest.mark.parametrize(
    "expected", ["需要保留完整转录。" * 5000, ""], ids=["long-text", "silence"]
)
async def test_upload_original_ogg_and_read_complete_text(audio_file: Path, expected: str) -> None:
    original = await asyncio.to_thread(audio_file.read_bytes)

    async def recognize(request: web.Request) -> web.Response:
        assert await read_upload(request, mime="audio/ogg") == original
        return web.json_response({"request_id": "r1", "text": expected, "duration_ms": 20})

    async with asr_server(recognize) as endpoint:
        assert (
            await DeepSeekASR(endpoint=endpoint, api_key="voice-secret").transcribe(audio_file)
            == expected
        )


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, '{"error":"private-upstream-secret"}'),
        (429, '{"error":"private-upstream-secret"}'),
        (201, '{"text":"private-upstream-secret"}'),
        (200, '{"result":{"text":"private-upstream-secret"}}'),
        (200, "private-upstream-secret"),
        (200, '{"text":42}'),
        (200, "[]"),
    ],
)
async def test_failure_does_not_expose_server_response(audio_file: Path, status: int, body: str):
    async def recognize(request: web.Request) -> web.Response:
        await request.read()
        return web.Response(text=body, status=status)

    async with asr_server(recognize) as endpoint:
        with pytest.raises(AudioTranscriptionError) as caught:
            await DeepSeekASR(endpoint=endpoint, api_key="voice-secret").transcribe(audio_file)
        assert "voice-secret" not in str(caught.value)
        assert "private-upstream-secret" not in str(caught.value)


async def test_redirect_not_followed(audio_file: Path) -> None:
    calls = 0

    async def recognize(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        await request.read()
        return web.Response(status=307, headers={"Location": "/v1/tools/asr"})

    async with asr_server(recognize) as endpoint:
        with pytest.raises(AudioTranscriptionError, match="HTTP"):
            await DeepSeekASR(endpoint=endpoint, api_key="voice-secret").transcribe(audio_file)
        assert calls == 1


async def test_response_limit(audio_file: Path) -> None:
    async def recognize(request: web.Request) -> web.Response:
        await request.read()
        return web.json_response({"text": "x" * 300})

    async with asr_server(recognize) as endpoint:
        client = DeepSeekASR(endpoint=endpoint, api_key="key", max_response_bytes=128)
        with pytest.raises(AudioTranscriptionError, match="response exceeds"):
            await client.transcribe(audio_file)


async def test_file_limit_before_upload(audio_file: Path) -> None:
    calls = 0

    async def recognize(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        return web.json_response({"text": ""})

    async with asr_server(recognize) as endpoint:
        client = DeepSeekASR(endpoint=endpoint, api_key="key", max_audio_bytes=1)
        with pytest.raises(AudioTranscriptionError):
            await client.transcribe(audio_file)
        assert calls == 0


async def test_cancellation_and_timeout(audio_file: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def recognize(request: web.Request) -> web.Response:
        await request.read()
        started.set()
        await release.wait()
        return web.json_response({"text": "late"})

    async with asr_server(recognize) as endpoint:
        try:
            task = asyncio.create_task(
                DeepSeekASR(endpoint=endpoint, api_key="key").transcribe(audio_file)
            )
            await asyncio.wait_for(started.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            client = DeepSeekASR(endpoint=endpoint, api_key="key", timeout=0.05)
            with pytest.raises(AudioTranscriptionError, match="timed out"):
                await client.transcribe(audio_file)
        finally:
            release.set()


def test_default_endpoint_and_independent_credentials() -> None:
    settings = DeepSeekASRSettings()
    assert settings.endpoint == "https://api.deepseek.com/v1/tools/asr"
    assert settings.resolve_api_key({"DEEPSEEK_API_KEY": "voice-key"}) == "voice-key"
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        settings.resolve_api_key({"ARK_API_KEY": "other", "AUDIO_SUMMARY_API_KEY": "other"})


@pytest.mark.parametrize(
    "options",
    [
        {"api_key": ""},
        {"api_key": "\r\nunsafe"},
        {"api_key": "key", "endpoint": "https://user:password@example.com"},
        {"api_key": "key", "timeout": float("inf")},
        {"api_key": "key", "max_audio_bytes": 0},
    ],
)
def test_invalid_configuration(options: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        DeepSeekASR(**options)  # type: ignore[arg-type]


@pytest.mark.parametrize("fail_second_part", [False, True], ids=["complete", "upstream-failure"])
async def test_device_upload_saves_only_complete_transcript(
    tmp_path: Path, fail_second_part: bool
) -> None:
    samples = 240 * 16000 + 320  # The device can upload up to 300 s in one segment.
    recording = tmp_path / "long-meeting.ogg"
    await asyncio.to_thread(write_opus, recording, samples, pause_seconds=(118, 119))
    sample_counts: list[int] = []
    texts = ("项目周五交付。", "", "负责人是小林。")

    async def recognize(request: web.Request) -> web.Response:
        data = await read_upload(request, mime="audio/wav")
        with wave.open(io.BytesIO(data), "rb") as wav:
            assert wav.getframerate() == 16000 and wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            samples = wav.getnframes()
            assert 0 < samples <= 120 * 16000
            assert len(wav.readframes(samples)) == samples * 2
        sample_counts.append(samples)
        if fail_second_part and len(sample_counts) == 2:
            return web.json_response({"error": "busy"}, status=503)
        return web.json_response({"text": texts[len(sample_counts) - 1]})

    class SummaryModel:
        def __init__(self) -> None:
            self.contexts: list[AgentContext] = []

        async def stream(self, context: AgentContext) -> AsyncIterator[ModelEvent]:
            self.contexts.append(context)
            yield AssistantMessage("小林负责周五交付。")

    model = SummaryModel()
    async with asr_server(recognize) as endpoint:
        config = AppConfig.model_validate(
            {
                "events": {
                    "poll_interval": 0.01,
                    "slices": {
                        "audio": {
                            "enabled": True,
                            "storage_path": "audio",
                            "asr": {"endpoint": endpoint, "api_key": "voice-secret"},
                            "summary_model": {"api_key": "summary-secret"},
                        }
                    },
                },
                "interfaces": {
                    "http": {
                        "enabled": True,
                        "port": 0,
                        "terminals": [
                            {
                                "terminal_id": "esp32",
                                "user_id": "user",
                                "conversation_id": "main",
                                "token": "device-secret",
                            }
                        ],
                    }
                },
            }
        )
        with SQLiteStore(tmp_path / "events.sqlite3") as store:
            async with (
                ConversationService(
                    config, factory.ModelSet(model, {}, model), store, base_dir=tmp_path
                ) as service,
                ClientSession() as client,
            ):
                assert service.http is not None
                host, port = service.http.addresses[0]
                response = await client.post(
                    f"http://{host}:{port}/v1/audio/slices",
                    data=await asyncio.to_thread(recording.read_bytes),
                    headers={
                        "Authorization": "Bearer device-secret",
                        "X-Terminal-ID": "esp32",
                        "Content-Type": "audio/ogg",
                        "X-Recording-ID": "meeting",
                        "X-Segment-Sequence": "1",
                        "X-Start-Sample": "0",
                        "X-Sample-Count": str(samples),
                        "X-Sample-Rate": "16000",
                    },
                )
                assert response.status == 202
                event_id = (await response.json())["event_id"]
                if fail_second_part:
                    async with asyncio.timeout(10):
                        while True:
                            status_response = await client.get(
                                f"http://{host}:{port}/v1/audio/slices/{event_id}",
                                headers={
                                    "Authorization": "Bearer device-secret",
                                    "X-Terminal-ID": "esp32",
                                },
                            )
                            assert status_response.status == 200
                            status = (await status_response.json())["status"]
                            if status != "pending":
                                break
                            await asyncio.sleep(0.01)
                    assert status == "retrying"
                    assert len(sample_counts) == 2
                    assert store.get_audio_transcript(event_id) is None
                    assert not model.contexts
                else:
                    await service.scheduler.wait_for_slices(timeout=10)
                    assert len(sample_counts) == 3 and sum(sample_counts) == samples
                    assert store.get_slice_status(event_id).status == "completed"
                    assert store.get_audio_transcript(event_id) == texts[0] + "\n" + texts[2]
                    (summary_input,) = model.contexts
                    assert texts[0] in repr(summary_input) and texts[2] in repr(summary_input)
    assert 118 * 16000 < sample_counts[0] < 119 * 16000
