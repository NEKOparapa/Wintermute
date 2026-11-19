import asyncio
import base64
import io
import json
import wave
from contextlib import asynccontextmanager

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from PIL import Image

from wintermute.agent import AgentContext
from wintermute.agent.core.types import ImageMessage
from wintermute.agent.providers.openai.responses import _input
from wintermute.application.models import open_models
from wintermute.application.service import ConversationService
from wintermute.config import AppConfig
from wintermute.events.storage.sqlite import SQLiteStore


def jpeg(color):
    output = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(output, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()


@asynccontextmanager
async def media_service(directory, *, model_reply=None):
    calls = []
    recordings = []

    async def asr(request):
        assert request.headers["Authorization"] == "Bearer local-asr"
        form = await request.post()
        assert form["file"].content_type == "audio/wav"
        with wave.open(form["file"].file, "rb") as wav:
            assert wav.getframerate() == 16000 and wav.getnchannels() == 1
            recordings.append(wav.getnframes())
        return web.json_response({"text": "这是转写草稿"})

    async def chat(request):
        data = await request.json()
        calls.append(data["messages"])
        choice = (
            model_reply(data)
            if model_reply
            else {"delta": {"content": "收到消息"}, "finish_reason": "stop"}
        )
        chunk = {"choices": [{"index": 0, **choice}]}
        return web.Response(
            text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
            content_type="text/event-stream",
        )

    upstream = web.Application(client_max_size=8 * 1024**2)
    upstream.router.add_post("/v1/tools/asr", asr)
    upstream.router.add_post("/v1/chat/completions", chat)
    async with TestServer(upstream) as server:
        config = AppConfig.model_validate(
            {
                "model": {
                    "api": "chat_completions",
                    "base_url": str(server.make_url("/v1")),
                    "model": "deepseek-flash",
                    "api_key": "local-chat",
                },
                "events": {
                    "database_path": ":memory:",
                    "slices": {
                        "audio": {
                            "asr": {
                                "endpoint": str(server.make_url("/v1/tools/asr")),
                                "api_key": "local-asr",
                            }
                        },
                    },
                },
                "interfaces": {
                    "http": {
                        "enabled": True,
                        "port": 0,
                        "terminals": [
                            {
                                "user_id": user,
                                "conversation_id": scope,
                                "terminal_id": term,
                                "token": term,
                            }
                            for term, user, scope in [
                                ("desktop", "u", "c"),
                                ("mobile", "u", "c"),
                                ("private", "u", "other"),
                                ("esp32", "v", "v-chat"),
                            ]
                        ],
                    }
                },
            }
        )
        with SQLiteStore(directory / "events.db") as store:
            async with open_models(config, base_dir=directory) as models:
                async with ConversationService(
                    config, models, store, base_dir=directory
                ) as service:
                    port = service.http.addresses[0][1]
                    yield f"http://127.0.0.1:{port}", calls, recordings


def test_media_conversation(tmp_path):
    async def check():
        async with (
            media_service(tmp_path) as (url, calls, recordings),
            ClientSession(url) as client,
        ):

            def auth(terminal):
                return {"X-Terminal-ID": terminal, "Authorization": "Bearer " + terminal}

            wav = io.BytesIO()
            with wave.open(wav, "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", ""))
                audio.writeframes(bytes(16000 * 2))
            speech_headers = {**auth("desktop"), "Content-Type": "audio/wav"}
            assert (await client.post("/v1/transcriptions")).status == 401
            assert (
                await client.post("/v1/transcriptions", data=b"bad", headers=speech_headers)
            ).status == 400
            response = await client.post(
                "/v1/transcriptions", data=wav.getvalue(), headers=speech_headers
            )
            assert await response.json() == {"text": "这是转写草稿"}
            assert recordings == [16000] and not calls

            images = [jpeg("red"), jpeg("blue")]
            message = {"message_id": "media", "text": "比较两张图", "images": images}

            async def submit(body):
                return await client.post("/v1/messages/images", json=body, headers=auth("desktop"))

            assert (
                await submit({**message, "images": ["data:image/jpeg;base64,YQ=="]})
            ).status == 400
            receipt = await (await submit(message)).json()
            assert "job_id" in receipt, receipt
            assert (await (await submit(message)).json())["duplicate"]
            assert (await submit({**message, "images": images[::-1]})).status == 409

            async def completed(receipt, terminal):
                for _ in range(100):
                    result = await (
                        await client.get("/v1/jobs/" + receipt["job_id"], headers=auth(terminal))
                    ).json()
                    if result["status"] in {"completed", "failed"}:
                        assert result["status"] == "completed", result
                        return
                    await asyncio.sleep(0.05)
                raise AssertionError("job timed out")

            await completed(receipt, "desktop")
            text = await client.post(
                "/v1/messages",
                json={"message_id": "follow", "text": "继续"},
                headers=auth("mobile"),
            )
            await completed(await text.json(), "mobile")
            for messages in (calls[0], calls[-1]):
                content = next(m["content"] for m in messages if isinstance(m["content"], list))
                assert content[0]["text"].startswith("比较两张图")
                assert [item["image_url"]["url"] for item in content[1:]] == images
            page = await (
                await client.get("/v1/history?view=dialogue", headers=auth("mobile"))
            ).json()
            files = next(row["files"] for row in page["items"] if row["id"] == receipt["event_id"])
            for file, image in zip(files, images, strict=True):
                route = "/v1/history/files/" + file["id"]
                assert await (
                    await client.get(route, headers=auth("mobile"))
                ).read() == base64.b64decode(image.split(",")[1])
                for terminal in ("private", "esp32"):
                    assert (await client.get(route, headers=auth(terminal))).status == 404
            device = await client.post(
                "/v1/messages",
                json={"message_id": "device", "text": "hello"},
                headers=auth("esp32"),
            )
            assert device.status == 202
            await completed(await device.json(), "esp32")

    asyncio.run(check())


def test_responses_visual_blocks():
    images = (jpeg("red"), jpeg("blue"))
    ctx = AgentContext("", (ImageMessage("比较", images=images),), ())
    assert _input(ctx)[0]["content"] == [
        {"type": "input_text", "text": "比较"},
        *[{"type": "input_image", "image_url": image} for image in images],
    ]
