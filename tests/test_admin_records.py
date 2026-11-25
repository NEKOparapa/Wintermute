import asyncio
import re
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from purin.config.api_keys import APIKeyRegistry
from purin.config.models import AppConfig, HTTPDeviceBinding, HTTPSettings
from purin.events.ingress import EventGateway
from purin.events.storage.sqlite import SQLiteStore
from purin.interfaces.http import HTTPInterface
from purin.interfaces.http_devices import HTTPDevices
from purin.memory.vault import MemoryVault


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
            settings, gateway, environ={"PURIN_ADMIN_PASSWORD": password}, base_dir=tmp_path
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
            "model": {"api_key_env": "TEST_PURIN_MAIN_KEY"},
            "events": {
                "database_path": ":memory:",
                "timeline_compression": {"ai_enabled": True, "summary_model": None},
            },
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
    (vault.root / "note" / "reading.md").write_text(
        "---\ntitle: Reading notes\n---\n\nA passage worth keeping.", encoding="utf-8"
    )
    with SQLiteStore(tmp_path / "events.sqlite") as store:
        interface = HTTPInterface(
            settings,
            EventGateway(store),
            api_keys=keys,
            memory_vaults={"alice": vault},
            environ={"PURIN_ADMIN_PASSWORD": password},
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
            for path in ("/admin/keys", "/admin/settings"):
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
            overview = await page.text()
            detail_url = "/admin/keys?key=env%3ATEST_PURIN_MAIN_KEY"
            assert overview.count(f'href="{detail_url}"') == 1
            assert 'name="api_key"' not in overview
            detail = await client.get(detail_url, headers=admin)
            assert detail.status == 200
            body = await detail.text()
            assert "主 Agent" in body and "活动时间线压缩" in body and "沿用主 Agent" in body
            match = re.search(r'name="csrf" value="([^"]+)"', body)
            assert match
            csrf = match.group(1)
            new_key = "test-replacement-1234"
            saved = await client.post(
                "/admin/keys",
                headers=admin,
                data={"csrf": csrf, "key_id": "env:TEST_PURIN_MAIN_KEY", "api_key": new_key},
            )
            assert saved.status == 200
            rendered = await saved.text()
            assert "••••1234" in rendered and new_key not in rendered
            assert keys.provider("main")() == new_key
            archive = await client.get("/admin/memory?path=note/reading.md", headers=admin)
            assert archive.status == 200
            body = await archive.text()
            assert "<h1>Reading notes</h1>" in body and "<p>A passage worth keeping.</p>" in body
            assert 'id="memory-schedule"' not in body
            assert "user=" not in body and 'name="user"' not in body
            library = await client.get("/admin/memory", headers=admin)
            body = await library.text()
            assert 'id="memory-directory"' in body and 'id="memory-preview"' in body
            assert "整理计划" not in body and "view=schedule" not in body
            assert (await client.get("/admin/memory?view=schedule", headers=admin)).status == 400
            settings_url = "/admin/settings"
            settings_page = await client.get(settings_url, headers=admin)
            assert settings_page.status == 200
            body = await settings_page.text()
            assert 'href="/admin/settings" aria-current="page"' in body
            assert "user=" not in body and 'name="user"' not in body
            assert all(label in body for label in ("整理间隔", "时间地区", "触发时间"))
            assert "下次整理" not in body and "保存计划" not in body
            assert 'value="Asia/Shanghai" selected' in body
            fields = {
                "csrf": csrf,
                "interval_days": "7",
                "timezone": "Europe/Paris",
                "local_time": "08:30",
            }
            assert (await client.post("/admin/memory", headers=admin, data=fields)).status == 405
            assert (await client.get("/admin/memory?user=other", headers=admin)).status == 400
            assert (
                await client.post(settings_url + "?user=other", headers=admin, data=fields)
            ).status == 400
            assert (
                await client.post(settings_url, headers=admin, data={**fields, "csrf": ""})
            ).status == 403
            for name in ("interval_days", "timezone", "local_time"):
                saved_field = await client.post(
                    settings_url,
                    headers={**admin, "Accept": "text/plain"},
                    data={"csrf": csrf, name: fields[name]},
                )
                assert saved_field.status == 204
            schedule = store.memory.schedule("alice")
            assert (schedule.interval_days, schedule.zone.key, schedule.local_time) == (
                7,
                "Europe/Paris",
                "08:30",
            )
            body = await (await client.get(settings_url, headers=admin)).text()
            assert all(
                value in body
                for value in ('value="7"', 'value="Europe/Paris" selected', 'value="08:30"')
            )
            for invalid in (
                {"timezone": "Invalid/Zone"},
                {"interval_days": "0"},
                {"local_time": "26:00"},
            ):
                rejected = await client.post(
                    settings_url, headers=admin, data={"csrf": csrf, **invalid}
                )
                assert rejected.status == 400 and store.memory.schedule("alice") == schedule
            saved = await client.post(
                settings_url, headers=admin, data=fields, allow_redirects=False
            )
            assert saved.status == 303 and saved.headers["Location"] == settings_url


def test_memory_schedule_timezone_upgrade(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite"
    with SQLiteStore(database):
        pass
    # Reproduce the schedule table from before per-account timezones.
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("DROP TABLE memory_schedules")
        db.execute(
            "CREATE TABLE memory_schedules (user_id TEXT PRIMARY KEY, interval_days INTEGER "
            "NOT NULL, local_time TEXT NOT NULL, next_date TEXT NOT NULL)"
        )
        db.executemany(
            "INSERT INTO memory_schedules VALUES (?, 7, '02:30', '2026-11-01')",
            [("alice",), ("bob",)],
        )

    def clock() -> datetime:
        return datetime(2026, 10, 31, 20, tzinfo=UTC)

    with SQLiteStore(database, clock=clock) as store:
        old = store.memory.schedule("alice")
        assert old.next_date == date(2026, 11, 1) and old.zone.key == "Asia/Shanghai"
        changed = store.memory.set_schedule("alice", 7, "02:30", "America/New_York")
        assert changed.next_run_at.astimezone(UTC) == datetime(2026, 11, 1, 7, 30, tzinfo=UTC)
        # An in-flight run in the previous zone must not advance the new local schedule.
        store.memory.finish_schedule("alice", old, None)
        assert store.memory.schedule("alice") == changed
        store.memory.finish_schedule("alice", changed, None)
        advanced = store.memory.schedule("alice")
        assert advanced.next_date == date(2026, 11, 8)
        assert store.memory.set_schedule("alice", 7, "02:30", "America/New_York") == advanced
    with SQLiteStore(database, clock=clock) as store:
        assert store.memory.schedule("alice") == advanced and store.memory.schedule("bob") == old
        store.memory.set_schedule("bob", local_time="04:15")
    with closing(sqlite3.connect(database)) as db:
        # Editing only the time must preserve the inherited timezone of an older schedule.
        assert db.execute(
            "SELECT interval_days,local_time,timezone FROM memory_schedules WHERE user_id='bob'"
        ).fetchone() == (7, "04:15", None)


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
