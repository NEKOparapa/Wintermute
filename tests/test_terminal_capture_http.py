"""Compatibility between desktop window spans posted as slices and as perceptions."""

from contextlib import asynccontextmanager
from datetime import datetime
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
