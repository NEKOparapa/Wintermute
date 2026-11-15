"""A sequence of JPEG uploads through the normal HTTP, model and history interfaces."""

import asyncio
import base64
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from aiohttp import ClientSession
from PIL import Image, ImageDraw

from wintermute.agent import AssistantMessage, UserMessage
from wintermute.config import AppConfig
from wintermute.config.factory import ModelSet, create_store
from wintermute.interfaces.service import ConversationService


def screen(badge: bool = False, new_line: bool = False, other: bool = False) -> bytes:
    frame = Image.new("RGB", (640, 400), "white")
    draw = ImageDraw.Draw(frame)
    draw.rectangle((0, 0, 170, 399), fill="#dadada")
    draw.text((205, 35), "Project team" if other else "QQ / contact", fill="black")
    draw.text((210, 95), "Invitation to family group?", fill="black")
    if badge:
        draw.rectangle((80, 80, 87, 87), fill="red")
    if new_line:
        draw.text((210, 130), "I have sent the link.", fill="black")
    output = io.BytesIO()
    frame.save(output, format="JPEG", quality=90, subsampling=0)
    return output.getvalue()


class Vision:
    def __init__(self):
        self.requests = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, context):
        self.requests.append(context)
        if len(self.requests) == 2:
            self.entered.set()
            await self.release.wait()
        original = {
            "speaker": "用户",
            "evidence": "可以，我稍后发邀请。",
            "text": "可以邀请对方加入家庭组。",
        }
        subject, facts = [
            ("好友", [original]),
            (
                "好友",
                [
                    {**original, "text": "已同意邀请对方。"},
                    {"speaker": "用户", "evidence": "邀请链接已发送。", "text": "已发送邀请链接。"},
                ],
            ),
            (
                "项目群",
                [{"speaker": "同事", "evidence": "周五开会。", "text": "群内出现了新的会议通知。"}],
            ),
        ][len(self.requests) - 1]
        yield AssistantMessage(
            json.dumps(
                {"subject": subject, "facts": facts, "noise_regions": [[120, 195, 145, 225]]},
                ensure_ascii=False,
            )
        )


class Main:
    def __init__(self):
        self.requests = []

    async def stream(self, context):
        self.requests.append(context)
        yield AssistantMessage("收到。")


async def test_periodic_screenshot_groups_http_runner_and_download(tmp_path: Path):
    terminals = [
        dict(terminal_id=t, user_id="owner", conversation_id=t, token=t) for t in ("work", "home")
    ]
    cfg = AppConfig.model_validate(
        {
            "events": {
                "database_path": str(tmp_path / "db"),
                "poll_interval": 0.01,
                "timeline_compression": {"ai_enabled": False},
                "slices": {
                    "image": {"enabled": True, "storage_path": "images", "model": {"model": "test"}}
                },
            },
            "interfaces": {"http": {"enabled": True, "port": 0, "terminals": terminals}},
        }
    )
    main, vision = Main(), Vision()
    capture = base64.urlsafe_b64encode(
        json.dumps({"platform": "windows", "app": "QQ", "title": "QQ"}).encode()
    ).decode()
    start = datetime(2026, 11, 15, 3, 20, tzinfo=UTC)
    with create_store(cfg.events) as store:
        async with (
            asyncio.timeout(15),
            ConversationService(
                cfg, ModelSet(main, {}, image_model=vision), store, base_dir=tmp_path
            ) as service,
            ClientSession(
                headers={"X-Terminal-ID": "work", "Authorization": "Bearer work"}
            ) as device,
        ):
            host, port = service.http.addresses[0]
            base = f"http://{host}:{port}"

            async def upload(data, seconds):
                response = await device.post(
                    base + "/v1/image/slices",
                    data=data,
                    headers={
                        "Content-Type": "image/jpeg",
                        "X-Event-ID": str(uuid4()),
                        "X-Capture-Context": capture,
                        "X-Occurred-At": (start + timedelta(seconds=seconds)).isoformat(),
                    },
                )
                assert response.status == 202
                return (await response.json())["event_id"]

            async def chat(text):
                message = await device.post(
                    base + "/v1/messages", json={"message_id": str(uuid4()), "text": text}
                )
                assert message.status == 202
                assert (
                    await service.scheduler.wait((await message.json())["job_id"])
                ).status == "completed"
                return [
                    m.text
                    for m in main.requests[-1].messages
                    if isinstance(m, UserMessage) and "来源：活动时间线" in m.text
                ]

            first = screen()
            badge = screen(badge=True)
            link = screen(badge=True, new_line=True)
            frame_ids = [await upload(first, 0), await upload(first, 15), await upload(badge, 30)]
            await service.scheduler.wait_for_slices(timeout=5)
            assert len(vision.requests) == 1
            frame_ids.append(await upload(link, 45))
            await vision.entered.wait()
            context = await chat("刚才 QQ 里谈到了什么？")
            assert len(context) == 1 and "可以邀请" in context[0] and "已发送" not in context[0]
            vision.release.set()
            await service.scheduler.wait_for_slices(timeout=5)
            context = await chat("有什么进展？")
            assert (
                len(context) == 2
                and "已发送邀请链接" in context[-1]
                and "可以邀请" not in context[-1]
                and "已同意邀请" not in context[-1]
            )
            await upload(screen(other=True), 60)
            await service.scheduler.wait_for_slices(timeout=5)
            assert len(vision.requests) == 3
            for event_id in frame_ids:
                status = await device.get(base + "/v1/image/slices/" + event_id)
                assert status.status == 200 and (await status.json())["status"] == "completed"

            history = base + "/v1/history"
            summary = await (
                await device.get(history, params={"view": "slice", "grouped": "1", "limit": "1"})
            ).json()
            assert (
                len(summary["items"]) == 1 and summary["items"][0]["group"]["subject"] == "项目群"
            )
            params = {
                "view": "slice",
                "grouped": "1",
                "limit": "1",
                "cursor": summary["next_cursor"],
            }
            previous = await (await device.get(history, params=params)).json()
            (group,) = previous["items"]
            assert previous["next_cursor"] is None and group["files"] == []
            assert group["group"]["frame_count"] == 4 and group["group"]["change_count"] == 2
            detail_url = history + "/screenshot-groups/" + group["group"]["id"]
            page = await (await device.get(detail_url, params={"limit": "2"})).json()
            end = await (
                await device.get(detail_url, params={"limit": "2", "cursor": page["next_cursor"]})
            ).json()
            frames = page["items"] + end["items"]
            assert end["next_cursor"] is None and [f["id"] for f in frames] == frame_ids[::-1]
            assert sum(bool(f["text"]) for f in frames) == 2
            assert all(len(f["files"]) == 1 for f in frames)
            (file,) = frames[0]["files"]
            assert file["name"].endswith(".jpg")
            downloaded = await device.get(history + "/files/" + file["id"])
            assert downloaded.status == 200 and await downloaded.read() == link
            outsider = {"X-Terminal-ID": "home", "Authorization": "Bearer home"}
            assert (await device.get(detail_url, headers=outsider)).status == 404
            assert (
                await device.get(history + "/files/" + file["id"], headers=outsider)
            ).status == 404
