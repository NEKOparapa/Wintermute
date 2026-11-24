import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from wintermute.events.core.activity import perception_activity, timeline_line
from wintermute.events.core.mobile_location import LocationFix
from wintermute.events.core.types import Device, EventError, EventInput
from wintermute.events.storage.sqlite import SQLiteStore
from wintermute.interfaces import photon


def test_mobile_fix_is_durable_and_address_is_attached_once(tmp_path):
    path = tmp_path / "events.db"
    fix = {
        "latitude": 30.12345678,
        "longitude": 120.12345678,
        "accuracy_m": 19.5,
        "kind": "fix",
        "coordinate_system": "wgs84",
    }
    event = EventInput(
        device_id="phone",
        producer_event_id="sample1",
        layer="perception",
        conversation_id="main",
        type="state.observed",
        occurred_at=datetime.now(UTC),
        payload={"subject": "device:phone", "state_key": "mobile.location", "value": fix},
    )
    with SQLiteStore(path) as store:
        store.register_device(Device(device_id="phone", user_id="owner"))
        store.register_conversation("main", "owner")
        invalid = event.model_copy(deep=True)
        invalid.payload["value"]["latitude"] = 91
        with pytest.raises(EventError):
            store.append(invalid)
        assert store.next_mobile_location() is None
        original = store.append(event).event
        assert store.append(event).duplicate
    with SQLiteStore(path) as store:
        assert store.next_mobile_location() == original
        store.finish_mobile_location(original.event_id, "示例路")
        store.finish_mobile_location(original.event_id, "重复结果")
        result = store.append(event)
        assert result.duplicate and result.event.payload["value"] == fix
        assert result.event.payload["map_location"]["address"] == "示例路"
        assert store.next_mobile_location() is None
        stages = store.history.perception_stages(
            "owner", "main", original.event_id, ai_enabled=False
        )
        assert "示例路" in stages["parts"][0]["text"]
        assert "20 米" in timeline_line(perception_activity(result.event))
        legacy = event.model_copy(deep=True, update={"producer_event_id": "old"})
        legacy.payload["value"] = "到达「公司」"
        store.append(legacy)
        assert store.next_mobile_location() is None


def test_only_resolved_locations_advance_device_baseline():
    start = datetime.now(UTC)
    with SQLiteStore() as store:
        store.register_device(Device(device_id="phone", user_id="owner"))
        store.register_device(Device(device_id="second", user_id="owner"))
        store.register_conversation("main", "owner")

        def report(seq, lat, device="phone"):
            return store.append(
                EventInput(
                    device_id=device,
                    producer_event_id=str(seq),
                    conversation_id="main",
                    layer="perception",
                    type="state.observed",
                    occurred_at=start + timedelta(minutes=20 * seq),
                    payload={
                        "subject": "device:" + device,
                        "state_key": "mobile.location",
                        "value": {
                            "latitude": lat,
                            "longitude": 110,
                            "accuracy_m": 15,
                            "coordinate_system": "wgs84",
                            "kind": "fix",
                            "wifi": {"state": "connected", "ssid": "Office"},
                        },
                    },
                )
            ).event

        first = report(0, 30)
        close = report(1, 30.002)
        far = report(2, 30.003)
        assert store.next_mobile_location() == first
        store.finish_mobile_location(first.event_id, "首站")
        assert store.next_mobile_location() == far
        assert (
            store.history.perception_stages("owner", "main", close.event_id, ai_enabled=False)[
                "filter_reason"
            ]
            == "location_nearby"
        )
        store.finish_mobile_location(far.event_id, None)
        again = report(3, 30.0035)
        assert store.next_mobile_location() == again
        store.finish_mobile_location(again.event_id, "次站")
        stages = store.history.perception_stages("owner", "main", again.event_id, ai_enabled=False)
        assert stages["parts"][0]["text"].splitlines()[1:] == [
            "位置：次站",
            "定位误差：15 米",
            "Wi-Fi：Office",
        ]
        other = report(4, 30.0035, "second")
        assert store.next_mobile_location() == other


def test_photon_uses_only_wgs84_and_preserves_chinese_address(monkeypatch):
    async def run():
        app = web.Application()

        async def reverse(request):
            assert set(request.query) == {"lat", "lon", "limit"}
            assert request.query["lat"] == "30.12345678"
            return web.json_response(
                {
                    "features": [
                        {
                            "properties": {
                                "state": "上海市",
                                "city": "上海市",
                                "district": "黄浦区",
                                "name": "公园",
                            }
                        }
                    ]
                }
            )

        app.router.add_get("/reverse", reverse)
        async with TestServer(app) as server, ClientSession() as session:
            monkeypatch.setattr(photon, "PHOTON_URL", str(server.make_url("/reverse")))
            fix = LocationFix(
                latitude=30.12345678,
                longitude=120.12345678,
                accuracy_m=20,
                coordinate_system="wgs84",
                kind="fix",
                wifi={"state": "connected", "ssid": "Office"},
            )
            assert await photon.resolve_location(session, fix) == "上海市 · 黄浦区 · 公园"
            assert (
                await photon.resolve_location(session, fix.model_copy(update={"accuracy_m": 1500}))
                == "上海市 · 黄浦区"
            )

    asyncio.run(run())
