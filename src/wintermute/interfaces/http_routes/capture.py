"""终端感知、音频、图像和文字切片。"""

import json
from collections.abc import Awaitable
from datetime import datetime

from aiohttp import web
from pydantic import AwareDatetime, JsonValue, field_validator

from wintermute.config.models import HTTPTerminalSettings
from wintermute.events import DuplicateEventError, EventError, EventGateway, Receipt
from wintermute.events.core.desktop import WINDOW_SPAN_STATE_KEY
from wintermute.interfaces.desktop_window import DIGEST_PREFIX

from .common import HTTP_INTERFACE, ClientId, HTTPError, HTTPInput, NonblankText, read_input

routes = web.RouteTableDef()


class _CaptureInput(HTTPInput):
    event_id: ClientId
    occurred_at: AwareDatetime | None = None

    @field_validator("occurred_at", mode="before")
    @classmethod
    def parse_iso_time(cls, value: object) -> object:
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return value


class _PerceptionInput(_CaptureInput):
    state_key: ClientId
    value: JsonValue


class _SliceInput(_CaptureInput):
    text: NonblankText


def _slice_binding(request: web.Request) -> HTTPTerminalSettings:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    if binding.is_desktop:
        raise HTTPError(410, "desktop_slice_removed")
    return binding


@routes.post("/v1/perceptions")
async def perception(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = interface.authenticate(request)
    data = await read_input(request, _PerceptionInput, "invalid_perception")
    if data.state_key in {"desktop.activity", "browser.page"} and binding.is_desktop:
        raise HTTPError(410, "desktop_observation_removed")
    return await _activity_receipt(_observe(interface.gateway, binding, data))


async def _observe(
    gateway: EventGateway, binding: HTTPTerminalSettings, data: _PerceptionInput
) -> Receipt:
    endpoint = gateway.perception.connect(
        binding.terminal_id, conversation_id=binding.conversation_id
    )
    try:
        return await endpoint.observe(
            data.state_key,
            data.value,
            producer_event_id=data.event_id,
            occurred_at=data.occurred_at,
        )
    except DuplicateEventError:
        if data.state_key != WINDOW_SPAN_STATE_KEY:
            raise
        old = gateway.store.received_event(binding.terminal_id, data.event_id)
        if (
            old is None
            or old.source_id != "default"
            or old.conversation_id != binding.conversation_id
            or old.user_id != binding.user_id
            or old.occurred_at != data.occurred_at
        ):
            raise
        # Screenshot consent can be revoked after the compound request was accepted
        # but before its ACK arrived. A retry without frames still names the same dwell.
        if old.type == "state.observed" and (old.correlation_id or "").startswith(DIGEST_PREFIX):
            value = old.payload.get("value")
            if old.payload.get("state_key") != data.state_key or not isinstance(value, dict):
                raise
            base = {k: v for k, v in value.items() if k != "visual_observations"}
            if json.dumps(data.value, sort_keys=True) != json.dumps(base, sort_keys=True):
                raise
            return Receipt(event=old, duplicate=True)
        raise


async def _activity_receipt(submission: Awaitable[Receipt]) -> web.Response:
    try:
        receipt = await submission
    except DuplicateEventError:
        raise HTTPError(409, "event_id_conflict") from None
    except (ValueError, OverflowError):
        raise HTTPError(400, "event_rejected") from None
    return web.json_response(
        {"event_id": receipt.event.event_id, "duplicate": receipt.duplicate}, status=202
    )


@routes.post("/v1/slices")
async def text_slice(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = _slice_binding(request)
    data = await read_input(request, _SliceInput, "invalid_slice")
    endpoint = interface.gateway.slice.connect(
        binding.terminal_id, conversation_id=binding.conversation_id
    )
    return await _activity_receipt(
        endpoint.record(data.text, producer_event_id=data.event_id, occurred_at=data.occurred_at)
    )


@routes.post("/v1/perceptions/desktop-window/batches")
@routes.post("/v1/perceptions/desktop-window")
async def desktop_window(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    return await interface.desktop_windows.submit(request, interface.authenticate(request))


@routes.post("/v1/audio/slices")
async def audio_submit(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    return await interface.audio.submit(request, _slice_binding(request))


@routes.post("/v1/image/slices")
async def image_submit(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    return await interface.images.submit(request, _slice_binding(request))


@routes.get("/v1/audio/slices/{event_id}")
@routes.get("/v1/image/slices/{event_id}")
async def media_status(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    binding = _slice_binding(request)
    event_id = request.match_info["event_id"]
    try:
        event = interface.gateway.store.get_event(event_id, user_id=binding.user_id)
    except EventError:
        raise HTTPError(404, "not_found") from None
    expected = "slice.audio" if request.path.startswith("/v1/audio/") else "slice.image"
    if (
        event.type != expected
        or event.terminal_id != binding.terminal_id
        or event.conversation_id != binding.conversation_id
    ):
        raise HTTPError(404, "not_found")
    status = interface.gateway.store.get_slice_status(event_id, user_id=binding.user_id)
    return web.json_response(status.model_dump(mode="json"))
