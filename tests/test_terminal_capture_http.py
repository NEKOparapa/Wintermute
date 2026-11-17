"""桌面采集 API 与其他终端的入口隔离。"""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from wintermute.config import HTTPSettings, HTTPTerminalSettings
from wintermute.events import EventGateway, SQLiteStore
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
                device_type="desktop" if terminal == "private" else None,
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


@pytest.mark.parametrize("terminal", ["desktop", "private"])
async def test_desktop_only_accepts_current_collection(terminal):
    with SQLiteStore(":memory:") as store:
        async with client_for(EventGateway(store)) as client:
            auth = headers(terminal)
            for route, body in [
                (
                    "/v1/perceptions",
                    {"state_key": "browser.page", "value": {"domain": "example.org"}},
                ),
                (
                    "/v1/perceptions",
                    {"state_key": "desktop.activity", "value": {"activity": "active"}},
                ),
                ("/v1/slices", {"text": TEXT}),
                ("/v1/slices", {"text": "主动分享的文字"}),
                ("/v1/slices", {"text": "网页选区 · 项目进展"}),
                ("/v1/image/slices", {}),
                ("/v1/audio/slices", {}),
            ]:
                response = await client.post(
                    route, headers=auth, json={"event_id": str(uuid4()), **body}
                )
                assert response.status == 410
            assert not store.list_events("slice", user_id="local")
            assert not store.list_events("perception", user_id="local")
            body = {
                "event_id": str(uuid4()),
                "occurred_at": END,
                "state_key": "desktop.window_span",
                "value": VALUE,
            }
            for duplicate in (False, True):
                response = await client.post("/v1/perceptions", headers=auth, json=body)
                assert response.status == 202
                assert (await response.json())["duplicate"] is duplicate
            assert len(store.list_events("perception", user_id="local")) == 1
            response = await client.post(
                "/v1/slices",
                headers=headers("phone"),
                json={"event_id": str(uuid4()), "text": "手机使用时段"},
            )
            assert response.status == 202
            assert len(store.list_events("slice", user_id="local")) == 1
