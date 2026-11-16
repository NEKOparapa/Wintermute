"""Run the idle worker beside a live main Agent with a deterministic clock/model."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from wintermute.agent import AssistantMessage
from wintermute.config import AppConfig
from wintermute.config.factory import ModelSet
from wintermute.events import SQLiteStore
from wintermute.events.runtime import profile_worker
from wintermute.interfaces.service import ConversationService


class ChatModel:
    def __init__(self):
        self.requests = []

    async def stream(self, context):
        self.requests.append(context)
        yield AssistantMessage("收到。")


class MeetingDreamer:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.batches = []

    async def stream(self, context):
        assert not context.tools
        conversations = json.loads(context.messages[0].text)["conversations"]
        self.batches.append([c["text"] for c in conversations])
        self.started.set()
        await self.release.wait()
        latest = conversations[-1]
        changes = []
        if "以后" in latest["text"]:
            changes.append(
                dict(
                    op="add",
                    section="长期偏好",
                    text=latest["text"],
                    quote=latest["text"],
                    source=latest["id"],
                )
            )
        yield AssistantMessage(json.dumps({"changes": changes}, ensure_ascii=False))


def config():
    return AppConfig.model_validate(
        {
            "interfaces": {
                "http": {
                    "enabled": True,
                    "port": 0,
                    "terminals": [
                        {
                            "user_id": "owner",
                            "terminal_id": "phone",
                            "conversation_id": "main",
                            "token": "test",
                        }
                    ],
                }
            },
            "events": {
                "poll_interval": 0.01,
                "profile_files": {"owner": "profiles/PROFILE.md"},
                "profile_dreaming": {"poll_interval": 0.015, "cold_start_idle_seconds": 2},
            },
        }
    )


async def wait_for(predicate):
    while not predicate():  # noqa: ASYNC110 -- observe a committed SQLite cursor
        await asyncio.sleep(0.01)


async def test_correction_during_dream_wins_and_restart_does_not_relearn(tmp_path):
    now = datetime(2026, 11, 16, 12, tzinfo=UTC)
    db = tmp_path / "service.db"
    path = tmp_path / "profiles/PROFILE.md"
    chat, dreaming = ChatModel(), MeetingDreamer()
    phone, desktop = "以后项目周会材料发手机。", "改一下，以后项目周会材料发电脑。"
    models = ModelSet(chat, {}, profile_summary=dreaming)
    with SQLiteStore(db, clock=lambda: now) as store:
        async with (
            asyncio.timeout(8),
            ConversationService(config(), models, store, base_dir=tmp_path) as service,
        ):
            ingress = service.gateway.conversation.connect("phone", "main")

            async def send(text):
                receipt = await ingress.send(text, occurred_at=now)
                assert (await service.scheduler.wait(receipt.job_id)).status == "completed"

            await send(phone)
            now += timedelta(seconds=3)
            await dreaming.started.wait()
            await send(desktop)
            assert len(chat.requests) == 2 and not path.exists()
            dreaming.release.set()
            now += timedelta(seconds=3)
            await wait_for(lambda: store.profile_status("owner").oldest_pending_at is None)
            markdown = path.read_text(encoding="utf-8")
            assert desktop in markdown and phone not in markdown
            assert dreaming.batches == [[phone], [phone, desktop]]
            await send("今天周会怎么安排？")
            assert desktop in chat.requests[-1].system_prompt
            assert all("wm-profile:" not in m.text for m in store.history("main", "owner"))
            # This last question is new and will be handled once, even across restarts.
            now += timedelta(seconds=3)
            await wait_for(lambda: store.profile_status("owner").oldest_pending_at is None)
            assert path.read_text(encoding="utf-8") == markdown

    with SQLiteStore(db, clock=lambda: now) as reopened:
        count = len(dreaming.batches)
        async with ConversationService(config(), models, reopened, base_dir=tmp_path) as service:
            await service.scheduler.profile_worker.process_user("owner")
        assert len(dreaming.batches) == count


async def test_hand_edited_section_survives_inflight_summary(tmp_path):
    path = tmp_path / "profiles/PROFILE.md"
    path.parent.mkdir()
    path.write_text("# 用户档案\n\n## 长期偏好\n- 只保留我认可的设置。\n", encoding="utf-8")
    now = datetime(2026, 11, 16, 6, tzinfo=UTC)
    model = MeetingDreamer()
    with SQLiteStore(":memory:", clock=lambda: now) as store:
        models = ModelSet(ChatModel(), {}, profile_summary=model)
        async with (
            asyncio.timeout(8),
            ConversationService(config(), models, store, base_dir=tmp_path) as service,
        ):
            receipt = await service.gateway.conversation.connect("phone", "main").send(
                "以后周会材料发手机。", occurred_at=now
            )
            await service.scheduler.wait(receipt.job_id)
            now += timedelta(seconds=3)
            await model.started.wait()
            edited = "# 用户档案\n\n## 长期偏好\n- 我自己决定发送设备。\n"
            path.write_text(edited, encoding="utf-8")
            model.release.set()
            await wait_for(lambda: store.profile_status("owner").oldest_pending_at is None)
            assert path.read_text(encoding="utf-8") == edited


async def test_restart_after_file_replace_before_database_commit(tmp_path, monkeypatch):
    now = datetime(2026, 11, 16, 12, tzinfo=UTC)
    path = tmp_path / "profiles/PROFILE.md"
    db = tmp_path / "service.db"
    dream = MeetingDreamer()
    dream.release.set()
    models = ModelSet(ChatModel(), {}, profile_summary=dream)
    replaced = asyncio.Event()
    original = profile_worker._replace_if_current

    def failed_commit(target, staged, expected):
        assert original(target, staged, expected)
        replaced.set()
        raise OSError("simulated stop after atomic replacement")

    with monkeypatch.context() as patch, SQLiteStore(db, clock=lambda: now) as store:
        patch.setattr(profile_worker, "_replace_if_current", failed_commit)
        async with (
            asyncio.timeout(8),
            ConversationService(config(), models, store, base_dir=tmp_path) as service,
        ):
            job = await service.gateway.conversation.connect("phone", "main").send(
                "以后周会材料发手机。", occurred_at=now
            )
            await service.scheduler.wait(job.job_id)
            now += timedelta(seconds=3)
            await replaced.wait()
            assert store.profile_status("owner").pending is not None
        saved = path.read_bytes()

    with SQLiteStore(db, clock=lambda: now) as store:
        async with (
            asyncio.timeout(8),
            ConversationService(config(), models, store, base_dir=tmp_path),
        ):
            await wait_for(lambda: store.profile_status("owner").oldest_pending_at is None)
            assert store.profile_status("owner").pending is None
            assert path.read_bytes() == saved and len(dream.batches) == 1
