from datetime import UTC, datetime, timedelta

from wintermute.agent import AgentRunner, AssistantMessage
from wintermute.config.models import ProfileDreamingSettings
from wintermute.events import EventGateway, RunScheduler, SQLiteStore, Terminal
from wintermute.events.runtime.profile_worker import ProfileWorker


class RecordingModel:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    async def stream(self, context):
        self.requests.append(context)
        yield AssistantMessage(self.answer)


async def complete_chat(gateway, occurred):
    scheduler = RunScheduler(gateway, lambda _: AgentRunner(RecordingModel("收到。")))
    async with scheduler:
        receipt = await gateway.conversation.connect("pc", "main").send(
            "这次评审用电脑。", occurred_at=occurred
        )
        assert (await scheduler.wait(receipt.job_id)).status == "completed"


def make_worker(store, path, model):
    gateway = EventGateway(store)
    gateway.register_terminal(Terminal(terminal_id="pc", user_id="owner"))
    gateway.register_conversation("main", "owner")
    worker = ProfileWorker(store, {"owner": path}, model, ProfileDreamingSettings(), timezone="UTC")
    return gateway, worker


async def test_historical_peak_waits_for_quiet_slot_but_day_old_backlog_runs_in_peak(tmp_path):
    model = RecordingModel('{"changes":[]}')
    now = datetime(2026, 11, 16, 8, 45, tzinfo=UTC)  # Monday
    path = tmp_path / "PROFILE.md"
    with SQLiteStore(tmp_path / "history.sqlite3", clock=lambda: now) as store:
        gateway, worker = make_worker(store, path, model)
        sensor = gateway.perception.connect("pc")
        for day in (9, 10, 11):
            for hour in (9, 11):
                start = now.replace(day=day, hour=hour, minute=0)
                end = start + timedelta(hours=1)
                await sensor.observe(
                    "desktop.window_span",
                    {
                        "foreground_app": "Editor",
                        "started_at": start.isoformat(),
                        "ended_at": end.isoformat(),
                        "duration_seconds": 3600,
                    },
                    occurred_at=end,
                )

        # With device activity only the worker publishes statistics without invoking the model.
        await worker.process_user("owner")
        assert "09:00–10:00、11:00–12:00" in path.read_text(encoding="utf-8")
        assert model.requests == []
        await complete_chat(gateway, now)
        now += timedelta(minutes=30)
        await worker.process_user("owner")
        assert model.requests == [] and store.profile_status("owner").cursor == 0
        now = now.replace(hour=10, minute=0)
        await worker.process_user("owner")
        assert len(model.requests) == 1
        assert store.profile_status("owner").cursor == store.profile_status("owner").latest_seq

        now = now.replace(hour=11, minute=0)
        await complete_chat(gateway, now)
        now += timedelta(hours=24) - timedelta(minutes=1)
        await complete_chat(gateway, now)
        await worker.process_user("owner")
        assert len(model.requests) == 1
        now += timedelta(minutes=1)
        # Tuesday 11:00 is a peak, but Monday's input has waited 24 hours.
        assert await worker.process_user("owner")
        assert len(model.requests) == 2


async def test_first_hour_of_chat_with_no_usage_history_then_no_repeat(tmp_path):
    now = datetime(2026, 11, 16, 10, tzinfo=UTC)
    model = RecordingModel('{"changes":[]}')
    path = tmp_path / "PROFILE.md"
    with SQLiteStore(":memory:", clock=lambda: now) as store:
        gateway, worker = make_worker(store, path, model)
        await complete_chat(gateway, now)
        now += timedelta(minutes=59)
        await worker.process_user("owner")
        assert model.requests == []
        now += timedelta(minutes=1)
        assert await worker.process_user("owner")
        assert len(model.requests) == 1 and not path.exists()
        now += timedelta(hours=2)
        assert not await worker.process_user("owner")
        assert len(model.requests) == 1
