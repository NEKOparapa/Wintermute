import asyncio
import base64
import io
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from pydantic import JsonValue, ValidationError

from wintermute.config.models import HTTPSettings
from wintermute.events.core.activity import (
    ActivityItem,
    activity_message,
    timeline_line,
    window_span_inputs,
)
from wintermute.events.core.perception_rules import (
    RuleDecision,
    filter_window,
    perception_line,
)
from wintermute.events.core.timeline_compression import (
    TimelinePolicy,
    batch_text,
    group_rules,
)
from wintermute.events.ingress import EventGateway
from wintermute.events.storage.sqlite import SQLiteStore
from wintermute.interfaces.desktop_window import _parse
from wintermute.interfaces.http import HTTPInterface


def _jpeg_base64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(32, 64, 96)).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_filter_window_keeps_short_visit_with_input_segments() -> None:
    short_plain: dict[str, JsonValue] = {
        "foreground_app": "WeChat",
        "started_at": "2026-11-23T10:00:00+08:00",
        "ended_at": "2026-11-23T10:00:05+08:00",
        "duration_seconds": 5,
        "work_objects": [
            {"occurred_at": "2026-11-23T10:00:00+08:00", "window_title": "张三"},
        ],
    }
    assert filter_window(short_plain) is None

    short_with_input: dict[str, JsonValue] = {
        **short_plain,
        "input_segments": [
            {
                "occurred_at": "2026-11-23T10:00:01+08:00",
                "ended_at": "2026-11-23T10:00:04+08:00",
                "text": "明天上午十点评审",
                "window_title": "张三",
            }
        ],
    }
    decisions: list[RuleDecision] = []
    kept = filter_window(short_with_input, decisions)
    assert kept is not None
    assert kept["duration_seconds"] == 5
    assert kept["work_objects"] == short_plain["work_objects"]
    assert all(row["kept"] for row in decisions)


def test_filter_window_keeps_short_title_with_input_but_drops_pass_through() -> None:
    value: dict[str, JsonValue] = {
        "foreground_app": "WeChat",
        "started_at": "2026-11-23T10:00:00+08:00",
        "ended_at": "2026-11-23T10:00:20+08:00",
        "duration_seconds": 20,
        "work_objects": [
            {"occurred_at": "2026-11-23T10:00:00+08:00", "window_title": "文件传输助手"},
            {"occurred_at": "2026-11-23T10:00:02+08:00", "window_title": "张三"},
            {"occurred_at": "2026-11-23T10:00:06+08:00", "window_title": "产品群"},
        ],
        "input_segments": [
            {
                "occurred_at": "2026-11-23T10:00:03+08:00",
                "ended_at": "2026-11-23T10:00:05+08:00",
                "text": "收到，马上改",
                "window_title": "张三",
            }
        ],
    }
    decisions: list[RuleDecision] = []
    kept = filter_window(value, decisions)
    assert kept is not None
    assert kept["duration_seconds"] == 18.0
    titles = [row["window_title"] for row in kept["work_objects"]]  # type: ignore[index]
    assert titles == ["张三", "产品群"]
    assert [row["kept"] for row in decisions] == [True, False, True, True]


def test_activity_and_timeline_render_input_segments() -> None:
    start = datetime.fromisoformat("2026-11-23T10:00:00+08:00")
    end = datetime.fromisoformat("2026-11-23T10:00:06+08:00")
    value: dict[str, JsonValue] = {
        "foreground_app": "Google Chrome",
        "started_at": start.isoformat(),
        "ended_at": end.isoformat(),
        "duration_seconds": 6,
        "work_objects": [
            {"occurred_at": "2026-11-23T10:00:00+08:00", "window_title": "搜索 - Google Chrome"},
        ],
        "input_segments": [
            {
                "occurred_at": "2026-11-23T10:00:01+08:00",
                "ended_at": "2026-11-23T10:00:03+08:00",
                "text": "wintermute uia",
                "window_title": "搜索 - Google Chrome",
            },
            {
                "occurred_at": "2026-11-23T10:00:05+08:00",
                "text": "第二段输入",
            },
        ],
        "input_segments_omitted": 3,
    }
    rendered = window_span_inputs(value, start, end, start.tzinfo or UTC)
    assert "输入记录：10:00:01–10:00:03「搜索」 wintermute uia" in rendered
    assert "中间略去 3 段输入" in rendered
    assert "10:00:05 第二段输入" in rendered

    item = ActivityItem(
        activity_id="evt-1",
        user_id="alice",
        conversation_id="c-1",
        device_id="desktop-1",
        source_id="default",
        kind="perception",
        activity_key="key-1",
        content={"subject": "device:desktop-1", "state_key": "desktop.window_span", "value": value},
        event_type="state.observed",
        occurred_at=end,
        known_at=end,
        source_event_ids=("evt-1",),
    )
    line = perception_line(item)
    assert "输入记录：10:00:01–10:00:03「搜索」 wintermute uia" in line
    assert "Google Chrome 10:00:00–10:00:06 6 秒" in timeline_line(item)
    assert "输入记录：" in activity_message(item).text

    policy = TimelinePolicy(timezone="Asia/Shanghai")
    batch = batch_text([item], policy)
    assert "输入记录：" in batch.rule_text
    assert "wintermute uia" in group_rules([batch], policy)


def test_desktop_window_parse_validates_input_segments() -> None:
    jpeg = _jpeg_base64()
    valid = (
        '{"event_id":"ev-1","occurred_at":"2026-11-23T10:00:10+08:00",'
        '"state_key":"desktop.window_span","value":{"foreground_app":"WeChat",'
        '"started_at":"2026-11-23T10:00:00+08:00","ended_at":"2026-11-23T10:00:10+08:00",'
        '"duration_seconds":10,"input_segments":[{"occurred_at":"2026-11-23T10:00:02+08:00",'
        '"ended_at":"2026-11-23T10:00:04+08:00","text":"晚上七点开会","window_title":"张三"}],'
        '"input_segments_omitted":1},"frames":[{"occurred_at":"2026-11-23T10:00:05+08:00",'
        f'"jpeg_base64":"{jpeg}"' + "}]}"
    )
    span, _ = _parse(valid.encode(), batched=False)
    assert span.value.input_segments is not None
    assert span.value.input_segments[0].text == "晚上七点开会"

    unordered = valid.replace(
        '"ended_at":"2026-11-23T10:00:04+08:00"',
        '"ended_at":"2026-11-23T10:00:12+08:00"',
    )
    with pytest.raises(ValidationError):
        _parse(unordered.encode(), batched=False)


def test_http_perception_and_desktop_window_accept_input_segments(tmp_path: Path) -> None:
    asyncio.run(_check_http_ingestion(tmp_path))


async def _check_http_ingestion(tmp_path: Path) -> None:
    settings = HTTPSettings.model_validate(
        {
            "enabled": True,
            "devices": [
                {
                    "device_id": "desktop-1",
                    "user_id": "alice",
                    "conversation_id": "room-1",
                    "token": "secret-token",
                    "device_type": "desktop",
                }
            ],
        }
    )
    with SQLiteStore(tmp_path / "events.sqlite") as store:
        gateway = EventGateway(store)
        interface = HTTPInterface(settings, gateway, base_dir=tmp_path)
        async with TestClient(TestServer(interface.create_app())) as client:
            headers = {
                "Authorization": "Bearer secret-token",
                "X-Device-ID": "desktop-1",
            }
            short_payload = {
                "event_id": "span-short-input",
                "occurred_at": "2026-11-23T10:00:04+08:00",
                "state_key": "desktop.window_span",
                "value": {
                    "foreground_app": "WeChat",
                    "started_at": "2026-11-23T10:00:00+08:00",
                    "ended_at": "2026-11-23T10:00:04+08:00",
                    "duration_seconds": 4,
                    "work_objects": [
                        {"occurred_at": "2026-11-23T10:00:00+08:00", "window_title": "张三"}
                    ],
                    "input_segments": [
                        {
                            "occurred_at": "2026-11-23T10:00:01+08:00",
                            "ended_at": "2026-11-23T10:00:03+08:00",
                            "text": "验证码 [已脱敏:验证码]",
                            "window_title": "张三",
                        }
                    ],
                },
            }
            resp = await client.post("/v1/perceptions", headers=headers, json=short_payload)
            assert resp.status == 202
            row = store._connection.execute(
                "SELECT filter_reason FROM perception_events WHERE event_id=?",
                ((await resp.json())["event_id"],),
            ).fetchone()
            assert row is not None and row["filter_reason"] is None

            framed_payload = {
                "event_id": "span-framed-input",
                "occurred_at": "2026-11-23T10:15:00+08:00",
                "state_key": "desktop.window_span",
                "value": {
                    "foreground_app": "Chrome",
                    "started_at": "2026-11-23T10:00:00+08:00",
                    "ended_at": "2026-11-23T10:15:00+08:00",
                    "duration_seconds": 900,
                    "input_segments": [
                        {
                            "occurred_at": "2026-11-23T10:01:00+08:00",
                            "text": "查询项目排期",
                            "window_title": "搜索",
                        }
                    ],
                },
                "frames": [
                    {
                        "occurred_at": "2026-11-23T10:10:00+08:00",
                        "jpeg_base64": _jpeg_base64(),
                    }
                ],
            }
            resp2 = await client.post(
                "/v1/perceptions/desktop-window", headers=headers, json=framed_payload
            )
            assert resp2.status == 202
