import json
import sqlite3
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from wintermute.agent import ToolContext
from wintermute.config.models import HTTPTerminalSettings
from wintermute.events import EventGateway, SQLiteStore, Terminal
from wintermute.events.integrations.notify_user import NotifyRequest, notify_user_tool
from wintermute.interfaces.history_files import HistoryFiles


def build_tool(store, workspace):
    bindings = []
    for terminal, user, room in [
        ("desk", "owner", "work"),
        ("phone", "owner", "work"),
        ("ink", "owner", "work"),
        ("personal", "owner", "other"),
        ("stranger", "guest", "guest-room"),
    ]:
        store.register_conversation(room, user)
        store.register_terminal(Terminal(terminal_id=terminal, user_id=user))
        bindings.append(
            HTTPTerminalSettings(
                terminal_id=terminal,
                user_id=user,
                conversation_id=room,
                token="fixture",
                display_name=terminal.title(),
            )
        )
    return notify_user_tool(
        store, "work", bindings, workspace, HistoryFiles(workspace, workspace).resolve
    )


async def send(tool, call="call-1", run="run-1", **changes):
    args = NotifyRequest.model_validate(
        {
            "operation": "send",
            "targets": ["phone", "desk"],
            "title": "会议材料",
            "attachments": ["附件/报价单.pdf", "议程.txt"],
            **changes,
        },
        strict=True,
    )
    return await tool.execute(args, ToolContext(call, 1, run))


def scope(terminal):
    return {"user_id": "owner", "conversation_id": "work", "terminal_id": terminal}


async def test_files_are_snapshots_with_separate_delivery_state_and_call_identity(tmp_path):
    (tmp_path / "附件").mkdir()
    first = tmp_path / "附件/报价单.pdf"
    second = tmp_path / "议程.txt"
    original = b"%PDF-1.6" + b"x" * 70000
    first.write_bytes(original)
    second.write_text("准备核对本周材料", encoding="utf-8")
    now = datetime(2026, 11, 16, tzinfo=UTC)
    expires = (now + timedelta(seconds=30)).isoformat()
    db = tmp_path / "test.db"
    with SQLiteStore(db, clock=lambda: now) as store:
        tool = build_tool(store, tmp_path)
        listed = await tool.execute(NotifyRequest(operation="list_targets"), ToolContext("list", 1))
        assert {t["name"] for t in json.loads(listed.content)["targets"]} == {
            "Phone",
            "Desk",
            "Ink",
        }
        sent = await send(tool, expires_at=expires)
        assert not sent.is_error, sent.content
        id = json.loads(sent.content)["id"]
        first.unlink()
        second.unlink()
        assert await send(tool, expires_at=expires) == sent
        assert (await send(tool, expires_at=expires, title="Different")).is_error
        for target in ("desk", "phone"):
            items, cursor = store.list_user_notifications(**scope(target), pending_only=True)
            (item,) = items
            assert item["id"] == id and not cursor and not item["expired"]
            assert item["received_at"] is None and item["read_at"] is None
            assert [f["name"] for f in item["attachments"]] == ["报价单.pdf", "议程.txt"]
        file_id = item["attachments"][0]["id"]
        assert store.list_user_notifications(**scope("ink")) == ([], None)
        for wrong in (
            scope("ink"),
            {**scope("desk"), "user_id": "guest"},
            {**scope("desk"), "conversation_id": "other"},
        ):
            with pytest.raises(LookupError):
                store.notification_file(**wrong, notification_id=id, file_id=file_id)
            with pytest.raises(LookupError):
                store.ack_user_notification(**wrong, notification_id=id)
        ack = store.ack_user_notification(**scope("desk"), notification_id=id)
        now += timedelta(seconds=5)
        read = store.ack_user_notification(**scope("desk"), notification_id=id, read=True)
        assert read["received_at"] == ack["received_at"] and read["read_at"] != ack["received_at"]
        assert store.list_user_notifications(**scope("desk"), unread_only=True) == ([], None)
        assert store.list_user_notifications(**scope("desk"), pending_only=True) == ([], None)
        later = await send(tool, run="run-2", attachments=[], body="另一场会议")
        assert json.loads(later.content)["id"] != id
        head, cursor = store.list_user_notifications(**scope("phone"), limit=1)
        assert head[0]["id"] == json.loads(later.content)["id"] and cursor
        tail, _ = store.list_user_notifications(**scope("phone"), limit=1, cursor=cursor)
        assert tail[0]["id"] == id
    with SQLiteStore(db, clock=lambda: now) as reopened:
        info, rowid = reopened.notification_file(
            **scope("phone"), notification_id=id, file_id=file_id
        )
        assert info["size_bytes"] == len(original)
        assert (
            reopened.read_notification_chunk(rowid, 0, 65536)
            + reopened.read_notification_chunk(rowid, 65536, 65536)
        ) == original
        now += timedelta(seconds=30)
        phone, _ = reopened.list_user_notifications(**scope("phone"), pending_only=True)
        assert phone[-1]["expired"] and phone[-1]["received_at"] is None
        assert len(reopened.list_user_notifications(**scope("phone"), unread_only=True)[0]) == 1


async def test_bad_target_or_file_rejects_whole_message_and_migrates_v13(tmp_path):
    db = tmp_path / "existing.db"
    with SQLiteStore(db) as store:
        store.register_conversation("existing", "owner")
    with sqlite3.connect(db) as old:
        for table in ("notification_deliveries", "notification_files", "user_notifications"):
            old.execute(f"DROP TABLE {table}")
        old.execute("PRAGMA user_version=13")
    work = tmp_path / "files"
    work.mkdir()
    (work / "议程.txt").write_text("ok", encoding="utf-8")
    with (work / "large").open("wb") as large:
        large.truncate(8 * 1024 * 1024 + 1)
    with SQLiteStore(db) as store:
        assert store.get_conversation_user("existing") == "owner"
        tool = build_tool(store, work)
        for params in (
            {"targets": ["desk", "personal"], "body": "拒绝", "attachments": []},
            {"targets": ["stranger"], "body": "拒绝", "attachments": []},
            {"attachments": ["议程.txt", "../existing.db"]},
            {"attachments": ["议程.txt", str(db)]},
            {"attachments": ["large"]},
            {"attachments": ["missing"]},
            {"attachments": [{"history_file_id": "unknown"}]},
        ):
            assert (await send(tool, **params)).is_error
            assert store.list_user_notifications(**scope("desk")) == ([], None)
        try:
            (work / "link").symlink_to(db)
        except OSError:
            pass  # Windows may require Developer Mode to create a symbolic link.
        else:
            assert (await send(tool, attachments=["link"])).is_error
        assert store.list_user_notifications(**scope("desk")) == ([], None)


async def test_attaching_history_media_checks_ownership_and_conversation(tmp_path):
    with SQLiteStore() as store:
        tool = build_tool(store, tmp_path)
        gateway = EventGateway(store)
        raw = b"jpeg fixture"
        digest = sha256(raw).hexdigest()
        photo = tmp_path / (digest + ".jpg")
        photo.write_bytes(raw)
        receipts = []
        for terminal, room in (("personal", "other"), ("stranger", "guest-room"), ("desk", "work")):
            receipts.append(
                await gateway.slice.connect(terminal, conversation_id=room).publish(
                    "slice.image",
                    {"sha256": digest},
                )
            )
        for receipt in receipts[:2]:
            assert (
                await send(tool, attachments=[{"history_file_id": receipt.event.event_id}])
            ).is_error
        recording = b"OggS"
        audio = sha256(recording).hexdigest()
        (tmp_path / (audio + ".ogg")).write_bytes(recording)
        audio_receipt = await gateway.slice.connect("desk", conversation_id="work").record_audio(
            {
                "file": audio + ".ogg",
                "sha256": audio,
                "bytes": 4,
                "recording_id": "review",
                "sequence": 1,
                "start_sample": 0,
                "samples": 320,
            }
        )
        result = await send(
            tool,
            attachments=[
                {"history_file_id": r.event.event_id} for r in (receipts[2], audio_receipt)
            ],
        )
        assert not result.is_error, result.content
        photo.unlink()
        (item,) = store.list_user_notifications(**scope("phone"))[0]
        assert [f["content_type"] for f in item["attachments"]] == ["image/jpeg", "audio/ogg"]
        for ref, data in zip(item["attachments"], [raw, recording], strict=True):
            metadata, row = store.notification_file(
                **scope("phone"), notification_id=item["id"], file_id=ref["id"]
            )
            assert metadata == ref and store.read_notification_chunk(row, 0, 100) == data
