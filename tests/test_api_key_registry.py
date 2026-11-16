"""Provider keys are grouped by effective source, durable, and rebound on the next request."""

import asyncio
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_asr_audio import write_opus

from wintermute.agent import AgentContext, UserMessage
from wintermute.config import AppConfig, ConfigError
from wintermute.config.api_keys import APIKeyRegistry
from wintermute.config.factory import configure_app_logging, open_models
from wintermute.events import SQLiteStore
from wintermute.interfaces.service import ConversationService
from wintermute.logging import get_logger


async def test_shared_keys_rotate_on_the_wire_without_touching_separate_visual_key(
    tmp_path, monkeypatch
):
    sdk = pytest.importorskip("openai")
    fake_key = "REGISTRY_FIXTURE"
    monkeypatch.setenv(fake_key, "first-main-key")
    seen = []

    async def receive(request):
        seen.append((request.path, request.headers["Authorization"]))
        await request.read()
        if request.path.endswith("asr"):
            return web.json_response({"text": "OK"})
        # Exercise actual SDK HTTP auth without needing completion fixtures.
        return web.json_response({"error": {"message": "fixture"}}, status=401)

    app = web.Application()
    app.router.add_post("/{path:.*}", receive)
    async with TestServer(app) as upstream:
        url = str(upstream.make_url("/v1"))
        config = AppConfig.model_validate(
            {
                "model": {"api": "chat_completions", "base_url": url, "api_key_env": fake_key},
                "events": {
                    "database_path": ":memory:",
                    "slices": {
                        "audio": {
                            "enabled": True,
                            "asr": {"api_key_env": fake_key, "endpoint": url + "/asr"},
                            "summary_model": {"api_key_env": fake_key},
                        },
                        "image": {
                            "enabled": True,
                            "model": {
                                "base_url": url,
                                "api_key_env": fake_key,
                                "api_key": "vision",
                            },
                        },
                    },
                },
                "interfaces": {
                    "http": {
                        "enabled": True,
                        "terminals": [
                            {
                                "terminal_id": "owner",
                                "user_id": "me",
                                "conversation_id": "main",
                                "token": "terminal-token",
                                "manage_api_keys": True,
                            }
                        ],
                    }
                },
            }
        )
        with SQLiteStore() as store:
            async with open_models(config) as models:
                service = ConversationService(config, models, store, base_dir=tmp_path)
                transcriber = service.scheduler.slice_processor.audio_processor.transcriber
                clip = tmp_path / "sample.ogg"
                write_opus(clip, 320)
                items = models.api_keys.list_items()
                assert items[0]["usages"] == ["主 Agent", "语音转录", "音频摘要", "活动时间线压缩"]
                assert items[1]["masked"] == "••••"
                context = AgentContext("", (UserMessage("hello"),), ())
                for value in ("first-main-key", "rotated-main-key"):
                    if seen:
                        models.api_keys.update(items[0]["id"], value)
                    for model in (models.main, models.image_model):
                        with pytest.raises(sdk.AuthenticationError):
                            _ = [event async for event in model.stream(context)]
                    assert await transcriber.transcribe(clip) == "OK"
                    assert seen[-3:] == [
                        ("/v1/chat/completions", "Bearer " + value),
                        ("/v1/responses", "Bearer vision"),
                        ("/v1/asr", "Bearer " + value),
                    ]
                assert os.environ[fake_key] == "first-main-key"


async def test_atomic_rotations_survive_event_reset_and_write_failure(tmp_path, monkeypatch):
    private = tmp_path / "private-config"
    monkeypatch.setenv("WINTERMUTE_SETTINGS_DIR", str(private))
    config = AppConfig.model_validate(
        {
            "model": {"api_key_env": "FIXTURE_MAIN"},
            "events": {
                "database_path": "events.sqlite3",
                "slices": {"image": {"enabled": True, "model": {"api_key_env": "FIXTURE_VISION"}}},
            },
            "logging": {"console": False, "file": "service.log"},
        }
    )
    db = tmp_path / "events.sqlite3"
    with SQLiteStore(db), configure_app_logging(config, base_dir=tmp_path):
        registry = APIKeyRegistry(config, base_dir=tmp_path)
        await asyncio.gather(
            asyncio.to_thread(registry.update, "env:FIXTURE_MAIN", "rotated-value-A"),
            asyncio.to_thread(registry.update, "env:FIXTURE_VISION", "rotated-value-B"),
        )
        get_logger().warning("upstream failure: %s", "rotated-value-A")
    db.unlink()
    assert len(list(private.rglob("api-keys.json"))) == 1
    persisted = next(private.rglob("api-keys.json"))
    if os.name == "posix":
        assert not persisted.stat().st_mode & 0o077
        assert not persisted.parent.stat().st_mode & 0o077
    with SQLiteStore(db), configure_app_logging(config, base_dir=tmp_path):
        restarted = APIKeyRegistry(config, base_dir=tmp_path)
        get_logger().warning("after restart: %s", "rotated-value-B")
        assert restarted.provider("main")() == "rotated-value-A"
        assert restarted.provider("vision")() == "rotated-value-B"

        def failed(*args):
            raise OSError("failed replace")

        monkeypatch.setattr(os, "replace", failed)
        with pytest.raises(OSError):
            restarted.update("env:FIXTURE_MAIN", "must-not-persist")
        assert restarted.provider("main")() == "rotated-value-A"
        assert (
            restarted.provider("main")()
            == APIKeyRegistry(config, base_dir=tmp_path).provider("main")()
        )
        assert list(persisted.parent.iterdir()) == [persisted]
    log = (tmp_path / "service.log").read_text(encoding="utf-8")
    assert "rotated-value" not in log and log.count("[REDACTED]") >= 2


async def test_missing_key_still_fails_startup_without_manager(monkeypatch, tmp_path):
    monkeypatch.delenv("NO_MANAGER_KEY_FIXTURE", raising=False)
    config = AppConfig.model_validate(
        {
            "model": {"api_key_env": "NO_MANAGER_KEY_FIXTURE"},
            "events": {"database_path": ":memory:"},
        }
    )
    with pytest.raises(ConfigError, match="missing"):
        async with open_models(config, base_dir=tmp_path):
            pytest.fail("Unmanaged service accepted a missing credential")
