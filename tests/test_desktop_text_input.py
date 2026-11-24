import asyncio
import base64
import hashlib
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
from wintermute.events.core.desktop_input import filter_input
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("验证码 482910", "验证码 [已脱敏:验证码]"),
        ("482 910", "[已脱敏:验证码]"),
        ("token='aBcDe12345'", "token='[已脱敏:Token]'"),
        ("Bearer AbCdEf1234567890", "Bearer [已脱敏:Token]"),
        ("密码是 Abc@123456，收到", "密码是 [已脱敏:密钥]，收到"),
        ("ghp_1234567890abcdef1234567890abcdef1234", "[已脱敏:密钥]"),
        (
            "https://test:example@localhost/?api_key=example",
            "https://test:[已脱敏:密钥]@localhost/?api_key=[已脱敏:密钥]",
        ),
        ("转到 4111 1111 1111 1111 吧", "转到 [已脱敏:银行卡] 吧"),
        ("银行卡号：6222 0212 3456 7890", "银行卡号：[已脱敏:银行卡]"),
        ("端口 8080，预算 50000 元", "端口 8080，预算 50000 元"),
    ],
)
def test_backend_redaction_is_idempotent(raw: str, expected: str) -> None:
    assert filter_input({"text": raw})[0] == expected
    assert filter_input({"text": expected}) == (expected, "kept")


def test_filtered_input_cannot_rescue_a_short_visit() -> None:
    at = "2026-11-24T08:32:20+08:00"
    value: dict[str, JsonValue] = {
        "foreground_app": "QQ",
        "started_at": at,
        "ended_at": "2026-11-24T08:32:24+08:00",
        "duration_seconds": 4,
        "input_segments": [
            {"occurred_at": at, "text": "🚀" * 501},
            {"occurred_at": at, "text": "编辑后", "field_length": 501},
            {"occurred_at": at, "text": "\x00\t"},
        ],
    }
    decisions: list[RuleDecision] = []
    assert filter_window(value, decisions) is None
    assert [d["action"] for d in decisions if d["kind"] == "input"] == [
        "too_long",
        "too_long",
        "empty",
    ]
    assert filter_input({"text": "🚀" * 500}) == ("🚀" * 500, "kept")


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
    assert [row["kept"] for row in decisions if row["kind"] == "title"] == [False, True, True]


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
    assert line == (
        "Google Chrome（6s）\n - 10:00:00 搜索 "
        "(text：wintermute uia → 中间略去 3 段输入 → 第二段输入)\ndesktop-1"
    )
    assert timeline_line(item) == activity_message(item).text == line

    policy = TimelinePolicy(timezone="Asia/Shanghai")
    batch = batch_text([item], policy)
    assert line in batch.rule_text
    assert line in group_rules([batch], policy)


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


def test_android_visit_upload_attaches_one_frame_to_perception(tmp_path: Path) -> None:
    async def run() -> None:
        settings = HTTPSettings.model_validate(
            {
                "enabled": True,
                "devices": [
                    {
                        "device_id": "phone",
                        "user_id": "owner",
                        "conversation_id": "room",
                        "token": "test-token",
                        "device_type": "mobile",
                    }
                ],
            }
        )
        with SQLiteStore(tmp_path / "events.sqlite") as store:
            interface = HTTPInterface(settings, EventGateway(store), base_dir=tmp_path)
            async with TestClient(TestServer(interface.create_app())) as client:
                headers = {"Authorization": "Bearer test-token", "X-Device-ID": "phone"}
                route = "/v1/perceptions/mobile-app"
                end = "2026-11-25T03:12:00Z"
                plain = {
                    "event_id": "app-usage:visit-1",
                    "state_key": "mobile.app_usage",
                    "occurred_at": end,
                    "value": {
                        "activity_id": "visit-1",
                        "foreground_app": "Clock",
                        "device_platform": "android",
                        "started_at": "2026-11-25T03:00:00Z",
                        "ended_at": end,
                        "duration_seconds": 720,
                        "is_home_app": False,
                    },
                }
                frame = {"jpeg_base64": _jpeg_base64(), "occurred_at": "2026-11-25T03:10:00Z"}
                payload = {**plain, "frames": [frame]}
                for frames in (
                    [{**frame, "occurred_at": "2026-11-25T03:09:59.999Z"}],
                    [frame, frame],
                ):
                    res = await client.post(
                        route, headers=headers, json={**plain, "frames": frames}
                    )
                    assert res.status == 400
                res = await client.post(route, headers=headers, json=payload)
                assert res.status == 202
                event_id = (await res.json())["event_id"]
                event = store.get_event(event_id)
                assert (
                    event.layer == "perception" and event.payload["state_key"] == "mobile.app_usage"
                )
                assert [item["id"] for item in interface.history_files.describe(event)] == [
                    f"{event_id}:frame:0"
                ]
                assert store.desktop.next_desktop_frame()[0].event_id == event_id
                for path, body in ((route, payload), ("/v1/perceptions", plain)):
                    res = await client.post(path, headers=headers, json=body)
                    assert res.status == 202 and (await res.json())["duplicate"]
                stage = store.history.perception_stages("owner", "room", event_id, ai_enabled=False)
                assert len(stage["parts"]) == 1 and "Clock（720s）" in stage["parts"][0]["text"]
                store.desktop.record_desktop_frame(event_id, 0, "时钟界面")
                updated = store.history.perception_stages(
                    "owner", "room", event_id, ai_enabled=False
                )
                assert any("时钟界面" in part["text"] for part in updated["parts"])

    asyncio.run(run())


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
                    "device_platform": "macos",
                    "device_name": "工作电脑",
                    "started_at": "2026-11-23T10:00:00+08:00",
                    "ended_at": "2026-11-23T10:15:00+08:00",
                    "duration_seconds": 900,
                    "input_segments": [
                        {
                            "occurred_at": "2026-11-23T10:01:00+08:00",
                            "text": "查询项目排期  OTP: 132456",
                            "field_length": 19,
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
            framed_id = (await resp2.json())["event_id"]
            stages = store.history.perception_stages("alice", "room-1", framed_id, ai_enabled=False)
            line = stages["parts"][0]["text"]
            assert line.endswith("\nmacos 工作电脑")
            assert "查询项目排期 OTP: [已脱敏:验证码]" in line
            assert stages["retained"]["value"]["input_segments"][0]["text"].endswith(
                "[已脱敏:验证码]"
            )

            # A batch uses the same captured identity and filtered projection as a single upload.
            frame = framed_payload["frames"][0]
            digest = hashlib.sha256(base64.b64decode(frame["jpeg_base64"])).hexdigest()
            manifest = hashlib.sha256(f"{frame['occurred_at']}\0\0{digest}\n".encode()).hexdigest()
            batch = {
                **framed_payload,
                "event_id": "batch-input",
                "offset": 0,
                "total_frames": 1,
                "manifest": manifest,
            }
            route = "/v1/perceptions/desktop-window/batches"
            assert (await client.post(route, headers=headers, json=batch)).status == 202
            retry = await client.post(route, headers=headers, json=batch)
            assert retry.status == 202 and (await retry.json())["duplicate"]

            # Electron uploads UTC timestamps. The history and compression views share
            # the configured timeline timezone and keep repeated visits to a title distinct.
            def stamp(clock: str) -> str:
                return f"2026-11-24T00:{clock}Z"

            visit = {
                "event_id": "qq-example",
                "occurred_at": stamp("33:49.875"),
                "state_key": "desktop.window_span",
                "value": {
                    "foreground_app": "QQ",
                    "started_at": stamp("32:20"),
                    "ended_at": stamp("33:49.875"),
                    "duration_seconds": 89.875,
                    "device_platform": "windows",
                    "device_name": "Rogair14",
                    "work_objects": [
                        {"occurred_at": stamp(clock), "window_title": title}
                        for clock, title in [
                            ("32:20", "QQ"),
                            ("32:32", "图片查看器"),
                            ("32:48", "QQ"),
                            ("33:03", "图片查看器"),
                            ("33:20", "QQ"),
                        ]
                    ],
                    "input_segments": [
                        {
                            "occurred_at": stamp("32:51"),
                            "window_title": "QQ",
                            "text": "嗯，那就直接付款就行",
                        },
                        {"occurred_at": stamp("33:20"), "text": "好的"},
                        {"occurred_at": stamp("33:26"), "text": "谢谢"},
                        {"occurred_at": stamp("33:30"), "text": "长文本" * 180},
                    ],
                },
            }
            response = await client.post("/v1/perceptions", headers=headers, json=visit)
            assert response.status == 202
            event_id = (await response.json())["event_id"]
            stages = store.history.perception_stages("alice", "room-1", event_id, ai_enabled=False)
            expected = (
                "QQ（89s）\n - 08:32:20 QQ\n - 08:32:32 图片查看器"
                "\n - 08:32:48 QQ (text：嗯，那就直接付款就行)\n - 08:33:03 图片查看器"
                "\n - 08:33:20 QQ (text：好的 → 谢谢)\nwindows Rogair14"
            )
            assert stages["parts"][0]["text"] == expected
            assert stages["retained"]["value"]["duration_seconds"] == 89.875
            assert [r["action"] for r in stages["decisions"] if r["kind"] == "input"] == [
                "kept",
                "kept",
                "kept",
                "too_long",
            ]
            merged = store._connection.execute(
                "SELECT line FROM merged_entries ORDER BY at DESC LIMIT 1"
            ).fetchone()
            assert merged["line"].endswith(expected)

            # A full queue of multibyte segments can exceed the ordinary 64 KiB JSON route.
            large = {
                **visit,
                "event_id": "qq-large",
                "value": {
                    **visit["value"],
                    "input_segments": [
                        {"occurred_at": stamp("32:51"), "text": "字" * 500} for _ in range(50)
                    ],
                },
            }
            assert (await client.post("/v1/perceptions", headers=headers, json=large)).status == 413
            metadata_route = "/v1/perceptions/desktop-window/metadata"
            accepted = await client.post(metadata_route, headers=headers, json=large)
            assert accepted.status == 202
            replay = await client.post(metadata_route, headers=headers, json=visit)
            assert replay.status == 202 and (await replay.json())["duplicate"]
