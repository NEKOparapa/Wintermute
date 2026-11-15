"""Run the main Agent tool and download the same immutable files on two selected devices."""

import asyncio
from datetime import UTC, datetime, timedelta

from aiohttp import ClientSession

from wintermute.agent import AssistantMessage, ToolCall, ToolResultMessage
from wintermute.config import AppConfig
from wintermute.config.factory import ModelSet
from wintermute.events import SQLiteStore
from wintermute.interfaces.service import ConversationService


async def test_selected_devices_receive_immutable_attachments_over_http(tmp_path):
    now = datetime(2026, 11, 16, 2, tzinfo=UTC)
    original = bytes(range(256)) * 513
    report = tmp_path / "会议材料.pdf"
    report.write_bytes(original)
    (tmp_path / "清单.csv").write_bytes(b"")

    class Reporter:
        async def stream(self, context):
            if isinstance(last := context.messages[-1], ToolResultMessage):
                assert last.name == "notify_user" and not last.is_error, last.content
                yield AssistantMessage("材料已发送。")
            else:
                assert "notify_user" in {tool.name for tool in context.tools}
                yield AssistantMessage(
                    tool_calls=(
                        ToolCall(
                            "meeting-files",
                            "notify_user",
                            {
                                "operation": "send",
                                "targets": ["phone", "ink"],
                                "title": "会前材料",
                                "attachments": ["会议材料.pdf", "清单.csv"],
                                "expires_at": (now + timedelta(minutes=1)).isoformat(),
                            },
                        ),
                    )
                )

    terminals = [
        {"terminal_id": name, "token": "test-" + name, "user_id": user, "conversation_id": room}
        for name, user, room in [
            ("desktop", "u", "room"),
            ("phone", "u", "room"),
            ("ink", "u", "room"),
            ("guest", "other", "guest-room"),
            ("private", "u", "other-room"),
        ]
    ]
    cfg = AppConfig.model_validate(
        {
            "interfaces": {"http": {"enabled": True, "port": 0, "terminals": terminals}},
            "events": {"poll_interval": 0.01, "lease_seconds": 900},
            "tools": {"cwd": str(tmp_path)},
        }
    )

    def auth(name):
        return {"Authorization": "Bearer test-" + name, "X-Terminal-ID": name}

    with SQLiteStore(tmp_path / "events.db", clock=lambda: now) as store:
        service = ConversationService(cfg, ModelSet(Reporter(), {}), store, base_dir=tmp_path)
        async with asyncio.timeout(12), service, ClientSession() as client:
            host, port = service.http.addresses[0]
            base = f"http://{host}:{port}/v1"
            inbox = base + "/notifications"
            assert (await client.get(inbox)).status == 401
            for query in ("?unread=1&pending=1", "?limit=0", "?cursor=unsafe", "?terminal_id=ink"):
                assert (await client.get(inbox + query, headers=auth("phone"))).status == 400
            async with client.post(
                base + "/messages",
                headers=auth("desktop"),
                json={"message_id": "send-docs", "text": "请发到手机和墨水屏。"},
            ) as r:
                assert r.status == 202
                assert (
                    await service.scheduler.wait((await r.json())["job_id"])
                ).status == "completed"

            async def items(name, query=""):
                async with client.get(inbox + query, headers=auth(name)) as r:
                    assert r.status == 200
                    return (await r.json())["items"]

            (item,) = await items("phone", "?pending=1")
            assert (await items("ink")) == [item] and item["read_at"] is None
            assert [f["name"] for f in item["attachments"]] == ["会议材料.pdf", "清单.csv"]
            assert [f["content_type"] for f in item["attachments"]] == [
                "application/pdf",
                "text/csv",
            ]
            assert [f["size_bytes"] for f in item["attachments"]] == [len(original), 0]
            address = inbox + "/" + item["id"]
            report.write_bytes(b"edited after sending")
            for index, expected in enumerate((original, b"")):
                url = address + "/files/" + item["attachments"][index]["id"]
                for name in ("phone", "ink"):
                    async with client.head(url, headers=auth(name)) as head:
                        assert head.status == 200 and head.content_length == len(expected)
                    async with client.get(url, headers=auth(name)) as file:
                        assert file.status == 200 and await file.read() == expected
                        assert file.headers["Content-Disposition"].startswith("attachment;")
                        assert file.headers["X-Content-Type-Options"] == "nosniff"
                for outsider in ("desktop", "private", "guest"):
                    assert await items(outsider) == []
                    assert (await client.get(url, headers=auth(outsider))).status == 404
                    assert (
                        await client.post(address + "/read", headers=auth(outsider))
                    ).status == 404
            assert (await client.post(address + "/received", headers=auth("phone"))).status == 200
            assert (
                await items("phone", "?pending=1") == []
                and len(await items("phone", "?unread=1")) == 1
            )
            assert len(await items("ink", "?pending=1")) == 1
            read = await (await client.post(address + "/read", headers=auth("phone"))).json()
            assert read["read_at"] and await items("phone", "?unread=1") == []
            now += timedelta(minutes=2)
            assert (await items("ink"))[0]["expired"] and await items("ink", "?unread=1") == []
            assert (await items("ink", "?pending=1"))[0]["read_at"] is None
            assert store.list_events("notification", user_id="u") == ()
