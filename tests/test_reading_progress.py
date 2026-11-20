import asyncio

from aiohttp import ClientSession
from test_chat_media import media_service

from wintermute.events.storage.sqlite import SQLiteStore


def test_esp_reading_progress_is_a_deduplicated_slice(tmp_path):
    def auth(device_id):
        return {"X-Device-ID": device_id, "Authorization": f"Bearer {device_id}"}

    metadata = {
        "kind": "reading_progress",
        "book_id": "f4321dbe3c54fa1",
        "title": "阅读笔记",
        "format": "txt",
        "offset_bytes": 240,
        "total_bytes": 960,
        "at_end": False,
    }
    body = {
        "event_id": "reading-fixture",
        "occurred_at": "2026-11-21T00:00:00Z",
        "text": "阅读进度更新：《阅读笔记》读到 25%。",
        "metadata": metadata,
    }

    async def check():
        async with (
            media_service(tmp_path) as (url, calls, _),
            ClientSession(url) as client,
        ):
            async def send(data, device_id="esp32"):
                return await client.post("/v1/slices", json=data, headers=auth(device_id))

            response = await send(body)
            assert response.status == 202
            receipt = await response.json()
            assert receipt["duplicate"] is False
            retry = await send(body)
            assert retry.status == 202
            assert await retry.json() == {"event_id": receipt["event_id"], "duplicate": True}
            conflict = await send({**body, "metadata": {**metadata, "offset_bytes": 300}})
            assert conflict.status == 409
            assert (await send(body, "desktop")).status == 410

            for _ in range(100):
                history = await client.get("/v1/history?view=slice", headers=auth("esp32"))
                assert history.status == 200
                if any(body["text"] in item["text"] for item in (await history.json())["items"]):
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("Reading progress did not appear in slice history")
            assert not calls
            return receipt["event_id"]

    event_id = asyncio.run(check())
    with SQLiteStore(tmp_path / "events.db") as store:
        event = store.get_event(event_id, user_id="v")
        assert event.layer == "slice" and event.type == "slice.recorded"
        assert event.payload == {"text": body["text"], "metadata": metadata}
