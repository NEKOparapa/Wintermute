"""Async user question tool, HTTP routes, and notification-layer delivery tests."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from wintermute.agent import ToolContext
from wintermute.config.models import HTTPSettings, HTTPTerminalSettings
from wintermute.events import EventGateway, SQLiteStore, Terminal
from wintermute.events.core.types import event_message
from wintermute.events.integrations.ask_user import AskUserRequest, ask_user_tool
from wintermute.events.storage.questions import QuestionConflict
from wintermute.interfaces.http import HTTPInterface

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    value = SQLiteStore(tmp_path / "events.sqlite", clock=lambda: NOW)
    value.register_terminal(Terminal(terminal_id="desktop", user_id="alice"))
    value.register_terminal(Terminal(terminal_id="mobile", user_id="alice"))
    value.register_terminal(Terminal(terminal_id="esp32", user_id="alice"))
    value.register_conversation("conv-1", "alice")
    yield value
    value.close()


def test_ask_user_tool_returns_pending_immediately_and_is_idempotent(store):
    tool = ask_user_tool(store, "conv-1", lambda: "desktop")
    req = AskUserRequest(
        title="部署环境确认",
        question="是否现在发布到生产环境？",
        options=["立即发布", "仅发布测试环境"],
        multi_select=False,
        allow_custom=True,
    )
    ctx = ToolContext(call_id="call-1", turn=1, run_id="run-1")

    res1 = asyncio.run(tool.execute(req, ctx))
    assert not res1.is_error
    data1 = json.loads(res1.content)
    assert data1["status"] == "pending"
    assert data1["id"].startswith("q_")

    # Same run_id + call_id + identical input returns same question idempotently.
    res2 = asyncio.run(tool.execute(req, ctx))
    assert not res2.is_error
    assert json.loads(res2.content)["id"] == data1["id"]

    # No notification event is emitted yet until the user answers.
    assert not store.list_jobs()


def test_answer_enters_notification_layer_and_formats_event_message(store):
    created = store.questions.create(
        user_id="alice",
        conversation_id="conv-1",
        run_id="run-1",
        call_id="call-1",
        title="技术方案选择",
        question="新模块采用哪种存储？",
        options=["SQLite", "PostgreSQL"],
        multi_select=False,
        allow_custom=True,
        reply_terminal_id="desktop",
    )

    answered = store.questions.answer(
        user_id="alice",
        conversation_id="conv-1",
        terminal_id="mobile",
        id=created.id,
        selected_options=["SQLite"],
        answer_text="优先本地部署",
    )
    assert answered.status == "answered"
    assert answered.answered_terminal_id == "mobile"
    assert answered.selected_options == ["SQLite"]
    assert answered.answer_text == "优先本地部署"

    # Idempotent replay with identical answer succeeds.
    replay = store.questions.answer(
        user_id="alice",
        conversation_id="conv-1",
        terminal_id="mobile",
        id=created.id,
        selected_options=["SQLite"],
        answer_text="优先本地部署",
    )
    assert replay == answered

    # Conflicting answer from another terminal raises QuestionConflict.
    with pytest.raises(QuestionConflict):
        store.questions.answer(
            user_id="alice",
            conversation_id="conv-1",
            terminal_id="esp32",
            id=created.id,
            selected_options=["PostgreSQL"],
        )

    # Answer injected a notification event and queued a dispatch job.
    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].layer == "notification"
    assert jobs[0].status == "queued"

    event = store.get_event(jobs[0].event_id)
    assert event.layer == "notification"
    assert event.type == "question.answered"
    assert event.correlation_id == created.id
    assert event.reply_terminal_id == "mobile"

    msg = event_message(event)
    assert msg.source == "notification"
    assert msg.origin == "后台通知 · 用户回答"
    assert "技术方案选择" in msg.text
    assert "SQLite" in msg.text
    assert "优先本地部署" in msg.text


def test_http_routes_multi_terminal_and_wakeup(tmp_path):
    store = SQLiteStore(tmp_path / "http.sqlite", clock=lambda: NOW)
    gateway = EventGateway(store)
    woke = []
    gateway.subscribe(lambda: woke.append(True))

    settings = HTTPSettings(
        enabled=True,
        terminals=[
            HTTPTerminalSettings(
                terminal_id="desktop",
                user_id="alice",
                conversation_id="conv-1",
                token=SecretStr("tok-desktop"),
            ),
            HTTPTerminalSettings(
                terminal_id="mobile",
                user_id="alice",
                conversation_id="conv-1",
                token=SecretStr("tok-mobile"),
            ),
            HTTPTerminalSettings(
                terminal_id="esp32",
                user_id="alice",
                conversation_id="conv-1",
                token=SecretStr("tok-esp32"),
            ),
        ],
    )
    interface = HTTPInterface(settings, gateway, base_dir=tmp_path)

    async def scenario() -> None:
        q = store.questions.create(
            user_id="alice",
            conversation_id="conv-1",
            run_id="run-1",
            call_id="call-1",
            title="会议改期",
            question="是否将下午会议推迟到 16:00？",
            options=["同意", "保持原时间"],
            multi_select=False,
            allow_custom=True,
        )
        async with TestClient(TestServer(interface.create_app())) as client:
            desktop_headers = {"X-Terminal-ID": "desktop", "Authorization": "Bearer tok-desktop"}
            mobile_headers = {"X-Terminal-ID": "mobile", "Authorization": "Bearer tok-mobile"}
            esp_headers = {"X-Terminal-ID": "esp32", "Authorization": "Bearer tok-esp32"}

            # All three terminals see the pending question.
            for headers in (desktop_headers, mobile_headers, esp_headers):
                resp = await client.get("/v1/questions?status=pending", headers=headers)
                assert resp.status == 200
                body = await resp.json()
                assert len(body["items"]) == 1
                assert body["items"][0]["id"] == q.id

            # ESP32 answers the question.
            ans = await client.post(
                f"/v1/questions/{q.id}/answer",
                headers=esp_headers,
                json={"selected_options": ["同意"], "answer_text": ""},
            )
            assert ans.status == 200
            ans_body = await ans.json()
            assert ans_body["status"] == "answered"
            assert ans_body["answered_terminal_id"] == "esp32"
            assert woke  # gateway.wakeup() was triggered

            # Pending list is now empty on desktop and mobile.
            resp = await client.get("/v1/questions?status=pending", headers=desktop_headers)
            assert (await resp.json())["items"] == []

            # Mobile trying to answer with a different choice gets 409.
            conflict = await client.post(
                f"/v1/questions/{q.id}/answer",
                headers=mobile_headers,
                json={"selected_options": ["保持原时间"], "answer_text": ""},
            )
            assert conflict.status == 409
            assert (await conflict.json())["error"] == "question_already_resolved"

            # Create a second question and test dismiss.
            q2 = store.questions.create(
                user_id="alice",
                conversation_id="conv-1",
                run_id="run-1",
                call_id="call-2",
                title="可选清理",
                question="是否清理缓存？",
                options=["清理"],
                allow_custom=False,
            )
            dis = await client.post(f"/v1/questions/{q2.id}/dismiss", headers=desktop_headers)
            assert dis.status == 200
            assert (await dis.json())["status"] == "dismissed"

            jobs = store.list_jobs()
            assert len(jobs) == 2
            types = [store.get_event(j.event_id).type for j in jobs]
            assert types == ["question.answered", "question.dismissed"]

    try:
        asyncio.run(scenario())
    finally:
        store.close()
