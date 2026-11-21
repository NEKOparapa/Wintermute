import asyncio
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from wintermute.config.api_keys import APIKeyRegistry
from wintermute.config.models import AppConfig, HTTPDeviceBinding, HTTPSettings
from wintermute.events.ingress import EventGateway
from wintermute.events.storage.sqlite import SQLiteStore
from wintermute.interfaces.http import HTTPInterface
from wintermute.interfaces.http_devices import HTTPDevices
from wintermute.memory.vault import MemoryVault


def test_admin_and_device_history_scopes(tmp_path: Path) -> None:
    """Admin reads select a conversation; device reads always use the device's binding."""
    asyncio.run(check_history_scopes(tmp_path))


async def check_history_scopes(tmp_path: Path) -> None:
    password = "fixture-admin-password"
    settings = HTTPSettings.model_validate(
        {
            "enabled": True,
            "admin": {"enabled": True},
            "devices": [
                {"device_id": user, "user_id": user, "conversation_id": room, "token": user}
                for user, room in (("alice", "first"), ("bob", "second"))
            ],
        }
    )
    with SQLiteStore(tmp_path / "events.sqlite") as store:
        gateway = EventGateway(store)
        interface = HTTPInterface(
            settings, gateway, environ={"WINTERMUTE_ADMIN_PASSWORD": password}, base_dir=tmp_path
        )
        for binding in settings.devices:
            await gateway.conversation.connect(binding.device_id, binding.conversation_id).send(
                binding.user_id
            )
        doc_id = "doc_" + "a" * 64
        document = ("alice", "first", doc_id)
        store.chat_files.claim(document, "fixture")
        store.chat_files.finish(document, "fixture", b"chat attachment", "chat attachment")
        async with TestClient(TestServer(interface.create_app())) as client:
            proxy = {"X-Forwarded-Proto": "https"}
            device = {"Authorization": "Bearer alice", "X-Device-ID": "alice"}
            admin_path = "/admin/records/c-second/history?view=conversation"
            guest = await client.get(admin_path, headers={**proxy, **device}, allow_redirects=False)
            assert guest.status == 303 and guest.headers["Location"] == "/admin/login"

            login = await client.post(
                "/admin/login",
                headers={**proxy, "Origin": f"https://{client.host}:{client.port}"},
                data={"password": password},
                allow_redirects=False,
            )
            assert login.status == 303
            # TestServer uses HTTP behind the simulated TLS proxy, so forward the Secure cookie.
            admin = {**proxy, "Cookie": login.cookies.output(header="", attrs=[]).strip()}
            selected = await client.get(admin_path, headers=admin)
            assert selected.status == 200
            assert [row["text"] for row in (await selected.json())["items"]] == ["bob"]
            public_path = "/v1/history?view=dialogue"
            assert (await client.get(public_path, headers=admin)).status == 401
            own = await client.get(public_path, headers=device)
            assert [row["text"] for row in (await own.json())["items"]] == ["alice"]
            attachment = await client.get(f"/v1/history/files/{doc_id}", headers=device)
            assert attachment.status == 200 and await attachment.read() == b"chat attachment"
            other = {"Authorization": "Bearer bob", "X-Device-ID": "bob"}
            assert (await client.get(f"/v1/history/files/{doc_id}", headers=other)).status == 404
            assert (await client.get("/admin/records/c-missing/items", headers=admin)).status == 404
            for view in ("conversation", "notification", "perception", "slice", "merged"):
                assert (await client.get(f"/v1/history?view={view}", headers=device)).status == 400
            for path in (
                "/v1/history/compression",
                "/v1/history/traces/test",
                "/v1/history/perceptions/test/stages",
                "/v1/records/items",
                "/v1/records/batches",
                "/v1/connectors/history",
                "/v1/terminal-tools/history",
            ):
                assert (await client.get(path, headers=device)).status in (404, 405), path


def test_settings_are_managed_through_admin(tmp_path: Path) -> None:
    asyncio.run(check_management_settings(tmp_path))


async def check_management_settings(tmp_path: Path) -> None:
    password = "fixture-admin-password"
    config = AppConfig.model_validate(
        {
            "model": {"api_key_env": "TEST_WINTERMUTE_MAIN_KEY"},
            "events": {"database_path": ":memory:"},
            "interfaces": {
                "http": {
                    "enabled": True,
                    "admin": {"enabled": True},
                    "devices": [
                        {
                            "device_id": "desktop",
                            "user_id": "alice",
                            "conversation_id": "first",
                            "token": "fixture-device-token",
                        }
                    ],
                }
            },
        }
    )
    settings = config.interfaces.http
    keys = APIKeyRegistry(config)
    assert keys.can_provision
    vault = MemoryVault(tmp_path / "vault")
    vault.initialize()
    with SQLiteStore(tmp_path / "events.sqlite") as store:
        interface = HTTPInterface(
            settings,
            EventGateway(store),
            api_keys=keys,
            memory_vaults={"alice": vault},
            environ={"WINTERMUTE_ADMIN_PASSWORD": password},
            base_dir=tmp_path,
        )
        async with TestClient(TestServer(interface.create_app())) as client:
            device = {"Authorization": "Bearer fixture-device-token", "X-Device-ID": "desktop"}
            for path, methods in (
                ("/v1/settings/api-keys", ("GET", "POST")),
                ("/v1/memory/schedule", ("GET", "POST")),
                ("/v1/memory/file", ("GET", "PUT", "DELETE")),
                ("/v1/memory/tree", ("GET",)),
                ("/v1/memory/logs", ("GET",)),
            ):
                for method in methods:
                    assert (await client.request(method, path, headers=device)).status == 404

            batch = await client.post(
                "/v1/records/batches",
                headers=device,
                json={
                    "record_type": "document",
                    "items": [{"occurred_at": "2025-01-01T00:00:00Z", "content": "fixture"}],
                },
            )
            assert batch.status == 202
            batch_id = (await batch.json())["batch_id"]
            progress = await client.get(f"/v1/records/batches/{batch_id}", headers=device)
            assert progress.status == 200 and (await progress.json())["accepted_items"] == 1

            proxy = {"X-Forwarded-Proto": "https", "Origin": f"https://{client.host}:{client.port}"}
            for path in ("/admin/keys", "/admin/memory?user=alice"):
                response = await client.post(
                    path, headers={**proxy, **device}, allow_redirects=False
                )
                assert response.status == 303 and response.headers["Location"] == "/admin/login"
            login = await client.post(
                "/admin/login", headers=proxy, data={"password": password}, allow_redirects=False
            )
            assert login.status == 303
            admin = {**proxy, "Cookie": login.cookies.output(header="", attrs=[]).strip()}
            page = await client.get("/admin/keys", headers=admin)
            assert page.status == 200
            match = re.search(r'name="csrf" value="([^"]+)"', await page.text())
            assert match
            csrf = match.group(1)
            new_key = "test-replacement-1234"
            saved = await client.post(
                "/admin/keys",
                headers=admin,
                data={"csrf": csrf, "key_id": "env:TEST_WINTERMUTE_MAIN_KEY", "api_key": new_key},
            )
            assert saved.status == 200
            rendered = await saved.text()
            assert "••••1234" in rendered and new_key not in rendered
            assert keys.provider("main")() == new_key
            plan = await client.post(
                "/admin/memory?user=alice",
                headers={**admin, "HX-Request": "true"},
                data={"csrf": csrf, "interval_days": "7", "local_time": "08:30"},
            )
            assert plan.status == 200
            schedule = store.memory.schedule("alice")
            assert (schedule.interval_days, schedule.local_time) == (7, "08:30")


def test_retired_settings_are_rejected() -> None:
    with pytest.raises(ValidationError) as error:
        AppConfig.model_validate(
            {
                "events": {"memory": {"idle_seconds": 900}},
                "interfaces": {
                    "http": {
                        "devices": [
                            {
                                "device_id": "desk",
                                "conversation_id": "chat",
                                "user_id": "alice",
                                "token": "fixture",
                                "manage_api_keys": True,
                            }
                        ]
                    }
                },
            }
        )
    assert {(e["loc"], e["type"]) for e in error.value.errors()} == {
        (("events", "memory", "idle_seconds"), "extra_forbidden"),
        (("interfaces", "http", "devices", 0, "manage_api_keys"), "extra_forbidden"),
    }


def test_persisted_device_permission_is_removed_once(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite"
    active = HTTPDeviceBinding(device_id="desk", conversation_id="chat", user_id="alice")
    archived = active.model_copy(update={"device_id": "phone"})
    with SQLiteStore(database) as store:
        devices = HTTPDevices(store, [], {})
        token = devices.register(active)
        retired_token = devices.register(archived)
        devices.archive(archived.device_id)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("PRAGMA user_version=36")
        for device_id, flag in (("desk", "true"), ("phone", "false")):
            db.execute(
                "UPDATE http_device_credentials SET binding=json_set(binding, '$.manage_api_keys', "
                "json(?)) WHERE device_id=?",
                (flag, device_id),
            )
    for _ in range(2):
        with SQLiteStore(database) as store:
            devices = HTTPDevices(store, [], {})
            assert devices.authenticate("desk", token) == active
            assert devices.authenticate("phone", retired_token) is None
            assert store.devices.http_credentials("phone")["phone"].binding == archived
