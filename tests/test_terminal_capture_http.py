"""Compatibility between desktop window spans posted as slices and as perceptions."""

import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from wintermute.config import HTTPSettings, HTTPTerminalSettings
from wintermute.events import EventGateway, SQLiteStore
from wintermute.events.core.desktop import legacy_window_span_value
from wintermute.interfaces.http import HTTPInterface

START = "2026-11-14T08:04:00.000Z"
END = "2026-11-14T08:04:10.500Z"
TEXT = f"桌面前台停留（采样估算）：{START} 至 {END}，约 11 秒。应用「Teams」，窗口标题「周会」。"
VALUE = {
    "foreground_app": "Teams",
    "window_title": "周会",
    "started_at": START,
    "ended_at": END,
    "duration_seconds": 11,
}


def headers(terminal: str = "desktop") -> dict[str, str]:
    return {"X-Terminal-ID": terminal, "Authorization": "Bearer secret-" + terminal}


@asynccontextmanager
async def client_for(gateway: EventGateway):
    settings = HTTPSettings(
        enabled=True,
        terminals=[
            HTTPTerminalSettings(
                terminal_id=terminal,
                user_id="local",
                conversation_id="work",
                token=SecretStr("secret-" + terminal),
            )
            for terminal in ["desktop", "private", "phone"]
        ],
    )
    interface = HTTPInterface(settings, gateway)
    try:
        async with TestClient(TestServer(interface.create_app())) as client:
            yield client
    finally:
        await interface.aclose()


@pytest.mark.parametrize("old_server", [False, True], ids=["new-receipt", "old-slice-receipt"])
async def test_same_window_retries_across_both_endpoints(old_server: bool) -> None:
    with SQLiteStore(":memory:") as store:
        gateway = EventGateway(store)
        async with client_for(gateway) as client:
            event_id = str(uuid4())
            capture = {"event_id": event_id, "occurred_at": END}
            if old_server:
                await gateway.slice.connect("desktop", conversation_id="work").record(
                    TEXT, producer_event_id=event_id, occurred_at=datetime.fromisoformat(END)
                )
            old_body = {**capture, "text": TEXT}
            new_body = {**capture, "state_key": "desktop.window_span", "value": VALUE}
            first = await client.post("/v1/slices", headers=headers(), json=old_body)
            assert first.status == 202
            receipt = await first.json()
            assert receipt["duplicate"] == old_server
            for route, body in [("/v1/perceptions", new_body), ("/v1/slices", old_body)]:
                retry = await client.post(route, headers=headers(), json=body)
                assert retry.status == 202
                assert await retry.json() == {**receipt, "duplicate": True}
            conflict = await client.post(
                "/v1/slices",
                headers=headers(),
                json={**capture, "text": TEXT.replace("周会", "评审")},
            )
            assert conflict.status == 409
            assert len(store.list_events("slice", user_id="local")) == int(old_server)
            assert len(store.list_events("perception", user_id="local")) == int(not old_server)
            if not old_server:
                event = store.get_event(receipt["event_id"])
                assert event.payload["state_key"] == "desktop.window_span"
                assert event.payload["value"] == VALUE
                assert not store.pending_slices()


async def test_custom_terminal_requires_desktop_activity_and_shares_stay_slices() -> None:
    with SQLiteStore(":memory:") as store:
        async with client_for(EventGateway(store)) as client:

            async def send_slice(text: str, terminal: str) -> str:
                response = await client.post(
                    "/v1/slices",
                    headers=headers(terminal),
                    json={"event_id": str(uuid4()), "occurred_at": END, "text": text},
                )
                assert response.status == 202
                return store.get_event((await response.json())["event_id"]).layer

            assert await send_slice(TEXT, "phone") == "slice"
            state = await client.post(
                "/v1/perceptions",
                headers=headers("private"),
                json={
                    "event_id": str(uuid4()),
                    "state_key": "desktop.activity",
                    "value": {"activity": "active"},
                },
            )
            assert state.status == 202
            assert await send_slice(TEXT, "private") == "perception"
            assert await send_slice(TEXT, "phone") == "slice"
            assert await send_slice("主动分享的文字\n" + TEXT, "desktop") == "slice"
            assert await send_slice("网页选区 · 会议\n" + TEXT, "desktop") == "slice"


async def test_snapshot_only_returns_a_durable_receipt_and_reserves_its_id(tmp_path: Path) -> None:
    db_path = tmp_path / "events.sqlite3"
    capture = {
        "event_id": str(uuid4()),
        "state_key": "desktop.activity",
        "value": {"activity": "active", "foreground_app": "Chrome", "window_title": "会议"},
    }
    with SQLiteStore(db_path) as store:
        gateway = EventGateway(store)
        async with client_for(gateway) as client:
            response = await client.post("/v1/perceptions", headers=headers(), json=capture)
            assert response.status == 202
            ack = await response.json()
            assert ack["duplicate"] is False
            for status in ("idle", "locked", "unknown"):
                response = await client.post(
                    "/v1/perceptions",
                    headers=headers(),
                    json={**capture, "event_id": str(uuid4()), "value": {"activity": status}},
                )
                assert response.status == 202
            assert not store.list_events("perception", user_id="local")
            assert not store.list_states(user_id="local", conversation_id="work")
            assert store.pending_timeline("work", "local") is None
    with SQLiteStore(db_path) as store:
        gateway = EventGateway(store)
        async with client_for(gateway) as client:
            response = await client.post("/v1/perceptions", headers=headers(), json=capture)
            assert await response.json() == {**ack, "duplicate": True}
            for route, changes in [
                (
                    "/v1/perceptions",
                    {"state_key": "desktop.activity", "value": {"activity": "idle"}},
                ),
                ("/v1/perceptions", {"state_key": "desktop.window_span", "value": VALUE}),
                ("/v1/slices", {"text": "hello"}),
            ]:
                response = await client.post(
                    route, headers=headers(), json={"event_id": capture["event_id"], **changes}
                )
                assert response.status == 409
            for key, value in [
                ("browser.page", {"title": "会议"}),
                ("desktop.activity", {"user_note": "explicit input"}),
            ]:
                event_id = str(uuid4())
                response = await client.post(
                    "/v1/perceptions",
                    headers=headers(),
                    json={"event_id": event_id, "state_key": key, "value": value},
                )
                assert response.status == 202
                response = await client.post(
                    "/v1/perceptions", headers=headers(), json={**capture, "event_id": event_id}
                )
                assert response.status == 409
            assert len(store.list_events("perception", user_id="local")) == 2


async def test_existing_snapshot_retry_and_late_input_keep_the_committed_transcript(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "old.db"
    key = "desktop.activity"
    value = {"activity": "active", "foreground_app": "Chrome"}
    event_id = str(uuid4())
    start = datetime.fromisoformat(START)
    with SQLiteStore(db_path) as store:
        gateway = EventGateway(store)
        async with client_for(gateway):
            source = gateway.perception.connect("desktop", conversation_id="work")
            old = await source.observe(key, value, producer_event_id=event_id, occurred_at=start)
            await gateway.conversation.connect("desktop", "work").send("会议")
            store.acquire_scheduler("runner")
            job = store.claim_next(defer_input=True)
            assert job
            store.absorb_session_inputs(job.job_id, messages=())
            transcript = store.history("work", "local")
            assert any("Chrome" in getattr(msg, "text", "") for msg in transcript)
            await source.observe(key, {**value, "foreground_app": "Mail"})
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE desktop_snapshot_receipts")
        db.execute("PRAGMA user_version=8")
    with SQLiteStore(db_path) as store:
        gateway = EventGateway(store)
        async with client_for(gateway) as client:
            retry = await client.post(
                "/v1/perceptions",
                headers=headers(),
                json={"event_id": event_id, "state_key": key, "value": value},
            )
            assert await retry.json() == {"event_id": old.event.event_id, "duplicate": True}
            assert store.pending_timeline("work", "local") is None
            await gateway.perception.connect("desktop", conversation_id="work").observe(
                "browser.page", {"title": "会议"}, occurred_at=start.replace(year=2025)
            )
            message = store.pending_timeline("work", "local")
            assert message is not None and "browser.page" in message.text
            assert "desktop.activity" not in message.text
            assert store.history("work", "local") == transcript


def test_window_parser_validates_identifier_times_duration_and_optional_title() -> None:
    event_id = str(uuid4())
    at = datetime.fromisoformat(END)
    no_title = TEXT.replace("，窗口标题「周会」", "")
    assert legacy_window_span_value(event_id, no_title, at) == {
        key: value for key, value in VALUE.items() if key != "window_title"
    }
    for identifier, text, ended_at in [
        ("unrelated-id", TEXT, at),
        (event_id, TEXT.replace("11 秒", "10 秒"), at),
        (event_id, TEXT.replace("08:04:00", "08:05:00"), at),
        (event_id, TEXT.replace("-11-14", "-13-14"), at),
        (event_id, TEXT, datetime.fromisoformat(START)),
        (event_id, TEXT, None),
    ]:
        assert legacy_window_span_value(identifier, text, ended_at) is None
