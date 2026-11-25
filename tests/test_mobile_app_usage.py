from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from purin.events.core.activity import perception_activity, timeline_line
from purin.events.core.perception_rules import is_mobile_home_activity, perception_line
from purin.events.core.timeline_compression import TimelinePolicy, batch_text
from purin.events.core.types import Device, EventInput
from purin.events.storage.sqlite import SQLiteStore


def test_mobile_usage_compression_uses_the_paired_name_and_local_interval() -> None:
    end = datetime(2026, 11, 24, 0, 33, 20, tzinfo=UTC)
    event = EventInput(
        device_id="phone-1",
        producer_event_id="usage-1",
        conversation_id="main",
        layer="perception",
        type="state.observed",
        occurred_at=end,
        payload={
            "subject": "device:phone-1",
            "state_key": "mobile.app_usage",
            "value": {
                "foreground_app": "QQ",
                "device_platform": "android",
                "duration_seconds": 89,
                "started_at": "2026-11-24T00:31:51Z",
                "ended_at": end.isoformat(),
            },
        },
    )
    with SQLiteStore(clock=lambda: end) as store:
        store.register_device(Device(device_id="phone-1", user_id="owner"))
        store.register_conversation("main", "owner")
        store.devices.observe("phone-1", "install-1", "winrt", platform="android")
        receipt = store.append(event)
        store.devices.observe("phone-1", "install-1", "new name", platform="android", rename=True)
        retry = store.append(event)
        assert retry.duplicate and retry.event == receipt.event
        item = perception_activity(receipt.event)
        zone = ZoneInfo("Asia/Shanghai")
        expected = "QQ（89s）\n08:31:51 → 08:33:20\nandroid winrt"
        assert perception_line(item, zone=zone) == timeline_line(item, zone=zone) == expected
        assert expected in batch_text([item], TimelinePolicy(timezone=zone.key)).rule_text

        # The phone may upload an old interval after both the phone and its profile were renamed.
        event.payload["value"]["device_name"] = "当时的名字"
        captured = event.model_copy(update={"producer_event_id": "usage-2"})
        receipt = store.append(captured)
        assert store.append(captured).duplicate
        assert receipt.event.payload["value"]["device_name"] == "当时的名字"
        home = captured.model_copy(deep=True, update={"producer_event_id": "home"})
        home.payload["value"].update(foreground_app="Home", is_home_app=True)
        ignored = store.append(home)
        assert store.append(home).duplicate
        stages = store.history.perception_stages(
            "owner", "main", ignored.event.event_id, ai_enabled=False
        )
        assert stages["filter_reason"] == "mobile_home"
        assert not stages["parts"]
        assert (
            store.list_states(user_id="owner", conversation_id="main")[0].event_id
            == receipt.event.event_id
        )


def test_zero_second_mobile_usage_is_raw_only() -> None:
    end = datetime.fromisoformat("2026-11-24T18:48:56.608+00:00")
    with SQLiteStore(clock=lambda: end) as store:
        store.register_device(Device(device_id="phone-1", user_id="owner"))
        store.register_conversation("main", "owner")
        for seconds in (1, 0, 0.608):
            event = EventInput(
                device_id="phone-1",
                producer_event_id=f"usage-{seconds}",
                conversation_id="main",
                layer="perception",
                type="state.observed",
                occurred_at=end,
                payload={
                    "subject": "device:phone-1",
                    "state_key": "mobile.app_usage",
                    "value": {
                        "foreground_app": "系统用户界面",
                        "is_home_app": False,
                        "duration_seconds": seconds,
                        "started_at": (
                            "2026-11-24T18:48:55.000Z"
                            if seconds == 1
                            else "2026-11-24T18:48:56.000Z"
                        ),
                        "ended_at": end.isoformat(),
                    },
                },
            )
            receipt = store.append(event)
            assert store.append(event).duplicate
            event_id = receipt.event.event_id
            stages = store.history.perception_stages("owner", "main", event_id, ai_enabled=False)
            if seconds == 1:
                assert stages["filter_reason"] is None and stages["parts"]
                kept_id = event_id
            else:
                assert store.get_event(event_id).payload == receipt.event.payload
                assert stages["filter_reason"] == "mobile_zero_duration"
                assert stages["retained"] is None
                assert not (stages["parts"] or stages["rounds"] or stages["context_messages"])
                assert (
                    store.list_states(user_id="owner", conversation_id="main")[0].event_id
                    == kept_id
                )


@pytest.mark.parametrize(
    ("value", "ignored"),
    [
        ({"foreground_app": "荣耀桌面"}, True),
        ({"foreground_app": "com.miui.home"}, True),
        ({"foreground_app": "My Home", "is_home_app": True}, True),
        ({"foreground_app": "远程桌面"}, False),
        ({"foreground_app": "工作桌面", "is_home_app": False}, False),
        ({"foreground_app": "计算器"}, False),
    ],
)
def test_launcher_filter_only_applies_to_mobile_usage(value: dict, ignored: bool) -> None:
    assert is_mobile_home_activity("mobile.app_usage", value) is ignored
    assert not is_mobile_home_activity("desktop.window_span", value)
