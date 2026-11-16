"""Authorization and transport boundaries for server-wide model credentials."""

import asyncio
import json
from unittest.mock import Mock

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from wintermute.config import AppConfig, HTTPSettings
from wintermute.config.factory import open_models
from wintermute.events import EventGateway, SQLiteStore
from wintermute.interfaces.http import HTTPInterface, _key_transport_allowed
from wintermute.interfaces.service import ConversationService

ROUTE = "/v1/settings/api-keys"
MANAGER = {"X-Terminal-ID": "owner-laptop", "Authorization": "Bearer owner-token"}
GUEST = {"X-Terminal-ID": "http-desktop", "Authorization": "Bearer guest-token"}


def settings(guest_token="guest-token"):
    return HTTPSettings.model_validate(
        {
            "enabled": True,
            "terminals": [
                {
                    "terminal_id": "owner-laptop",
                    "user_id": "local",
                    "conversation_id": "work",
                    "token": "owner-token",
                    "manage_api_keys": True,
                },
                {
                    "terminal_id": "http-desktop",
                    "user_id": "local",
                    "conversation_id": "work",
                    "token": guest_token,
                },
            ],
        }
    )


def test_reject_shared_management_token():
    with SQLiteStore(":memory:") as store, pytest.raises(ValueError, match="unique bearer token"):
        HTTPInterface(settings("owner-token"), EventGateway(store))


async def test_key_settings_authorize_one_terminal_and_never_cache_responses():
    registry = Mock()
    original = [{"id": "main", "usages": ["主模型"], "configured": False, "masked": None}]
    updated = [{**original[0], "configured": True, "masked": "••••1212"}]
    registry.list_items.return_value = original
    registry.update.return_value = updated
    with SQLiteStore(":memory:") as store:
        http = HTTPInterface(settings(), EventGateway(store), api_keys=registry)
        async with TestClient(TestServer(http.create_app())) as client:
            for credentials, status in (
                ({}, 401),
                ({**GUEST, "X-Terminal-ID": "owner-laptop"}, 401),
                (GUEST, 403),
            ):
                for method in ("GET", "POST"):
                    result = await client.request(method, ROUTE, headers=credentials)
                    assert result.status == status
                    assert "no-store" in result.headers["Cache-Control"]
            registry.list_items.assert_not_called()
            registry.update.assert_not_called()
            result = await client.get(ROUTE, headers=MANAGER)
            assert result.status == 200
            assert await result.json() == {"items": original}
            assert "no-store" in result.headers["Cache-Control"]
            key = "sample-key-1212"
            result = await client.post(ROUTE, headers=MANAGER, json={"id": "main", "api_key": key})
            assert result.status == 200
            assert await result.json() == {"items": updated}
            assert key not in await result.text()
            assert "no-store" in result.headers["Cache-Control"]
            registry.update.assert_called_once_with("main", key)
            for error, code, status in (
                (KeyError(key), "unknown_key_id", 404),
                (ValueError(key), "invalid_api_key", 400),
                (OSError(key), "api_key_management_unavailable", 503),
            ):
                registry.update.side_effect = error
                result = await client.post(
                    ROUTE,
                    headers=MANAGER,
                    json={"id": "wrong", "api_key": key},
                )
                assert result.status == status
                assert await result.json() == {"error": code}
                assert "no-store" in result.headers["Cache-Control"]
            registry.update.reset_mock()
            result = await client.post(
                ROUTE,
                headers=MANAGER,
                json={"id": "main", "api_key": key, "base_url": key},
            )
            assert result.status == 400 and key not in await result.text()
            result = await client.post(
                ROUTE,
                headers={**MANAGER, "Host": "public.example"},
                json={"id": "main", "api_key": key},
            )
            assert result.status == 403
            assert await result.json() == {"error": "secure_transport_required"}
            registry.update.assert_not_called()


@pytest.mark.parametrize(
    ("remote", "headers", "permitted"),
    [
        ("203.0.113.10", {"Host": "127.0.0.1", "X-Forwarded-Proto": "https"}, False),
        ("::1", {"Host": "[::1]:8080"}, True),
        ("127.0.0.1", {"Host": "wintermute.example", "X-Forwarded-Proto": "https"}, True),
        ("127.0.0.1", {"Host": "127.0.0.1", "X-Forwarded-Proto": "http"}, False),
        ("127.0.0.1", {"Host": "127.0.0.1", "X-Forwarded-Proto": "https,http"}, False),
        ("127.0.0.1", {"Host": "127.0.0.1", "X-Forwarded-For": "203.0.113.10"}, False),
    ],
)
async def test_only_trust_forwarded_https_from_loopback(remote, headers, permitted):
    request = make_mocked_request("GET", ROUTE, headers=headers).clone(remote=remote)
    assert _key_transport_allowed(request) is permitted


async def test_running_service_provisions_initial_key_and_restores_it_after_restart(
    tmp_path,
    monkeypatch,
):
    pytest.importorskip("openai")
    monkeypatch.delenv("WINTERMUTE_PROVISION_TEST", raising=False)
    monkeypatch.setenv("WINTERMUTE_SETTINGS_DIR", str(tmp_path / "private"))
    http_settings = settings().model_copy(update={"port": 0})
    config = AppConfig.model_validate(
        {
            "model": {"api_key_env": "WINTERMUTE_PROVISION_TEST"},
            "events": {"database_path": "events.sqlite3"},
            "interfaces": {"http": http_settings},
        }
    )
    key = "new-test-key-3141"
    for started_before in (False, True):
        with SQLiteStore(tmp_path / "events.sqlite3") as store:
            async with open_models(config, base_dir=tmp_path) as models:
                service = ConversationService(config, models, store, base_dir=tmp_path)
                async with service, ClientSession(headers=MANAGER) as client:
                    host, port = service.http.addresses[0]
                    url = f"http://{host}:{port}{ROUTE}"
                    response = await client.get(url)
                    assert response.status == 200
                    (item,) = (await response.json())["items"]
                    assert "主 Agent" in item["usages"]
                    assert item["configured"] is started_before
                    assert item["masked"] == ("••••3141" if started_before else None)
                    if not started_before:
                        response = await client.post(url, json={"id": item["id"], "api_key": key})
                        assert response.status == 200 and key not in await response.text()
                    assert models.api_keys.provider("main")() == key


@pytest.mark.parametrize("protocol", ("chat_completions", "responses"))
async def test_upstream_credential_echo_is_scrubbed_before_job_is_saved(tmp_path, protocol):
    pytest.importorskip("openai")
    key = "bad'fixture\\key"
    started, release = asyncio.Event(), asyncio.Event()

    async def echo(request):
        supplied = request.headers["Authorization"].removeprefix("Bearer ")
        started.set()
        await release.wait()
        detail = f'Invalid credential "{supplied}"; request reference abc'
        if protocol == "chat_completions":
            return web.json_response({"error": {"message": detail}}, status=401)
        event = {"type": "error", "code": "invalid_key", "message": detail, "sequence_number": 0}
        return web.Response(text=f"data: {json.dumps(event)}\n\n", content_type="text/event-stream")

    upstream = web.Application()
    upstream.router.add_post("/{path:.*}", echo)
    async with TestServer(upstream) as server:
        config = AppConfig.model_validate(
            {
                "model": {
                    "api": protocol,
                    "base_url": str(server.make_url("/v1")),
                    "api_key": "initial",
                },
                "events": {"database_path": ":memory:"},
                "interfaces": {"http": settings().model_copy(update={"port": 0})},
            }
        )
        with SQLiteStore(":memory:") as store:
            async with open_models(config, base_dir=tmp_path) as models:
                service = ConversationService(config, models, store, base_dir=tmp_path)
                async with service, ClientSession(headers=MANAGER) as client:
                    host, port = service.http.addresses[0]
                    base = f"http://{host}:{port}"
                    slot = models.api_keys.list_items()[0]["id"]
                    result = await client.post(base + ROUTE, json={"id": slot, "api_key": key})
                    assert result.status == 200
                    result = await client.post(
                        base + "/v1/messages",
                        json={"message_id": "test-401", "text": "hi"},
                    )
                    job_id = (await result.json())["job_id"]
                    try:
                        async with asyncio.timeout(5):
                            await started.wait()
                        result = await client.post(
                            base + ROUTE,
                            json={"id": slot, "api_key": "next-fixture-key"},
                        )
                        assert result.status == 200
                    finally:
                        release.set()
                    async with asyncio.timeout(5):
                        await service.scheduler.wait_until_idle()
                    error = store.get_job(job_id).error
                    assert 'Invalid credential "[REDACTED]"; request reference abc' in error
                    assert key not in error
                    if protocol == "chat_completions":
                        assert "AuthenticationError" in error and "401" in error
                    response = await client.get(base + "/v1/jobs/" + job_id)
                    assert response.status == 200 and key not in await response.text()
