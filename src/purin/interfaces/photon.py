"""Resolve committed phone fixes through the public Photon reverse geocoder."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from purin.events.core.mobile_location import LocationFix
from purin.events.storage.sqlite import SQLiteStore
from purin.logging import get_logger

logger = get_logger(__name__)
PHOTON_URL = "https://photon.komoot.io/reverse"


class PhotonBusy(Exception):
    def __init__(self, wait: int = 0) -> None:
        self.wait = wait


async def resolve_location(session: ClientSession, fix: LocationFix) -> str | None:
    params = {"lat": str(fix.latitude), "lon": str(fix.longitude), "limit": "1"}
    async with session.get(PHOTON_URL, params=params, allow_redirects=False) as response:
        if response.status in (429, 503):
            retry = response.headers.get("Retry-After", "")
            raise PhotonBusy(int(retry) if retry.isdecimal() and len(retry) < 9 else 60)
        response.raise_for_status()
        data = await response.json()
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise ValueError("invalid Photon response")
    if not features:
        return None
    props = features[0].get("properties")
    if not isinstance(props, dict):
        raise ValueError("invalid Photon feature")
    fields = ["state", "county", "city", "district"]
    if fix.accuracy_m < 1000:
        fields += ["locality", "street", "housenumber", "name"]
    parts = [
        " ".join(v.split()) for k in fields if isinstance(v := props.get(k), str) and v.strip()
    ]
    return " · ".join(dict.fromkeys(parts)) or None


class MobileGeocoder:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    async def run(self, session: ClientSession) -> None:
        delay = 0
        while True:
            event = self.store.next_mobile_location()
            if event is None:
                await asyncio.sleep(2)
                continue
            try:
                fix = LocationFix.model_validate(event.payload["value"])
                address = await resolve_location(session, fix)
            except (ClientError, TimeoutError, ValueError, PhotonBusy) as exc:
                logger.warning("Photon 地点解析暂缓", extra={"reason": type(exc).__name__})
                delay = max(
                    min(300, max(5, delay * 2)), exc.wait if isinstance(exc, PhotonBusy) else 0
                )
                await asyncio.sleep(delay)
            else:
                self.store.finish_mobile_location(event.event_id, address)
                delay = 0
                await asyncio.sleep(2)  # Also pace a burst of offline samples on reconnect.

    async def lifetime(self, app: web.Application) -> AsyncIterator[None]:
        async with ClientSession(
            timeout=ClientTimeout(total=15),
            headers={"User-Agent": "Purin/1.0 (mobile location)"},
        ) as session:
            task = asyncio.create_task(self.run(session))
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
