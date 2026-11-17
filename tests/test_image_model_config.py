"""Screenshot identification has its own endpoint, model and credentials."""

import asyncio
import base64
import io
import json
from pathlib import Path
from uuid import uuid4

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from PIL import Image
from pydantic import ValidationError

from wintermute.config import AppConfig, ConfigError, ImageModelSettings, ImageSliceSettings
from wintermute.config.factory import configure_app_logging, open_models
from wintermute.events import SQLiteStore
from wintermute.interfaces.service import ConversationService
from wintermute.logging import get_logger


def test_enabled_screenshots_require_independent_model() -> None:
    with pytest.raises(ValidationError, match="image.model"):
        ImageSliceSettings(enabled=True)
    settings = ImageModelSettings()
    assert settings.api_key_env == "VISION_API_KEY"
    with pytest.raises(ConfigError):
        settings.resolve_api_key({"ARK_API_KEY": "main-only"})


async def test_disabled_screenshots_need_no_visual_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VISION_API_KEY", raising=False)
    config = AppConfig.model_validate(
        {"model": {"api_key": "main-only"}, "events": {"slices": {"image": {"model": {}}}}}
    )
    async with open_models(config) as models:
        assert models.image_model is None


def test_visual_credentials_are_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISION_API_KEY", "vision-env-secret")
    config = AppConfig.model_validate(
        {
            "events": {"slices": {"image": {"model": {"api_key": "vision-inline-secret"}}}},
            "logging": {"console": False, "file": "vision.log"},
        }
    )
    assert "vision-inline-secret" not in str(config.redacted_dict())
    with configure_app_logging(config, base_dir=tmp_path):
        get_logger().info("two values: %s and %s", "vision-inline-secret", "vision-env-secret")
    log = (tmp_path / "vision.log").read_text(encoding="utf-8")
    assert "two values: [REDACTED] and [REDACTED]" in log


async def test_screenshot_and_chat_reach_their_own_model_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VISION_API_KEY", "vision-secret")
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(image, "JPEG")
    description = "会议清单包含断网重试。"
    requests = []

    async def vision(request: web.Request) -> web.Response:
        requests.append((request.path, request.headers["Authorization"], await request.json()))
        output = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": description}],
        }
        event = {
            "type": "response.completed",
            "response": {"status": "completed", "output": [output]},
        }
        return web.Response(text=f"data: {json.dumps(event)}\n\n", content_type="text/event-stream")

    async def chat(request: web.Request) -> web.Response:
        requests.append((request.path, request.headers["Authorization"], await request.json()))
        choice = {"index": 0, "delta": {"content": "看到了"}, "finish_reason": "stop"}
        return web.Response(
            text=f"data: {json.dumps({'choices': [choice]})}\n\ndata: [DONE]\n\n",
            content_type="text/event-stream",
        )

    upstream = web.Application()
    upstream.router.add_post("/vision/responses", vision)
    upstream.router.add_post("/main/chat/completions", chat)
    async with TestServer(upstream) as server:
        config = AppConfig.model_validate(
            {
                "model": {
                    "api": "chat_completions",
                    "base_url": str(server.make_url("/main")),
                    "model": "main-text",
                    "api_key": "main-secret",
                },
                "events": {
                    "poll_interval": 0.01,
                    "slices": {
                        "image": {
                            "enabled": True,
                            "storage_path": "images",
                            "model": {
                                "api": "responses",
                                "base_url": str(server.make_url("/vision")),
                                "model": "vision-only",
                            },
                        }
                    },
                },
                "interfaces": {
                    "http": {
                        "enabled": True,
                        "port": 0,
                        "terminals": [
                            {
                                "terminal_id": "mobile",
                                "user_id": "u",
                                "conversation_id": "c",
                                "token": "device-secret",
                            }
                        ],
                    }
                },
            }
        )
        with SQLiteStore() as store:
            async with (
                open_models(config) as models,
                ConversationService(config, models, store, base_dir=tmp_path) as service,
                ClientSession(
                    headers={
                        "Authorization": "Bearer device-secret",
                        "X-Terminal-ID": "mobile",
                    }
                ) as device,
            ):
                assert models.image_model is not models.main
                assert service.http is not None
                host, port = service.http.addresses[0]
                base = f"http://{host}:{port}"
                accepted = await device.post(
                    base + "/v1/image/slices",
                    data=image.getvalue(),
                    headers={
                        "Content-Type": "image/jpeg",
                        "X-Event-ID": str(uuid4()),
                        "X-Capture-Context": base64.urlsafe_b64encode(
                            b'{"platform":"android","app":"Editor"}'
                        ).decode(),
                        "X-Occurred-At": "2026-11-14T01:00:00Z",
                    },
                )
                assert accepted.status == 202
                screenshot_id = (await accepted.json())["event_id"]
                await service.scheduler.wait_for_slices(timeout=5)
                assert store.get_slice_status(screenshot_id).status == "completed"
                message = await device.post(
                    base + "/v1/messages",
                    json={"message_id": "question", "text": "我刚才在看什么？"},
                )
                assert message.status == 202
                async with asyncio.timeout(5):
                    job = await service.scheduler.wait((await message.json())["job_id"])
                assert job.status == "completed"
    visual, main = requests
    assert visual[:2] == ("/vision/responses", "Bearer vision-secret")
    assert visual[2]["model"] == "vision-only"
    screenshot_url = "data:image/jpeg;base64," + base64.b64encode(image.getvalue()).decode("ascii")
    assert {"type": "input_image", "image_url": screenshot_url} in visual[2]["input"][-1]["content"]
    assert main[:2] == ("/main/chat/completions", "Bearer main-secret")
    assert main[2]["model"] == "main-text"
    text = json.dumps(main[2]["messages"], ensure_ascii=False)
    assert description in text and "我刚才在看什么？" in text
    assert screenshot_url not in text
