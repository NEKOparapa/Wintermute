"""Redeem short-lived administrator invitations over HTTPS."""

import re
from collections import deque
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from secrets import randbelow
from time import monotonic
from typing import TYPE_CHECKING, Never

from aiohttp import web
from pydantic import Field

from purin.config.models import DEVICE_PLATFORM_TYPES, HTTPDeviceBinding, HTTPDeviceType
from purin.events.storage.repositories.devices import DeviceAlreadyRegistered

from .common import HTTP_INTERFACE, HTTPError, HTTPInput, read_input, trusted_https

if TYPE_CHECKING:
    from purin.interfaces.http import HTTPInterface

routes = web.RouteTableDef()
PAIRING_MINUTES = 5


class PairingInput(HTTPInput):
    code: str = Field(pattern=r"^[0-9]{5}-?[0-9]{5}$", max_length=11, repr=False)
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$", max_length=43, repr=False)
    installation: str = Field(pattern=r"^[A-Za-z0-9_-]{12,96}$", max_length=96)
    device_type: HTTPDeviceType
    device_name: str | None = Field(
        default=None, min_length=1, max_length=80, pattern=r"^[^\p{Cc}]+$"
    )


@dataclass
class Invitation:
    expires: float
    digest: bytes | None = None
    device_id: str | None = None
    installation: str | None = None


class PairingCodes:
    def __init__(self) -> None:
        self._codes: dict[str, Invitation] = {}
        self._rejected: deque[float] = deque()

    def _expire(self, now: float) -> None:
        self._codes = {key: item for key, item in self._codes.items() if item.expires > now}

    def _check_limit(self, now: float) -> None:
        while self._rejected and self._rejected[0] <= now - 60:
            self._rejected.popleft()
        if len(self._rejected) >= 20:
            raise HTTPError(429, "pairing_rate_limited")

    def _reject(self, now: float) -> Never:
        self._rejected.append(now)
        raise HTTPError(403, "invalid_pairing_code")

    def create(self) -> str:
        now = monotonic()
        self._expire(now)
        if len(self._codes) >= 32:
            self._codes.pop(next(iter(self._codes)))
        while True:
            code = f"{randbelow(10**10):010d}"
            if code not in self._codes:
                break
        self._codes[code] = Invitation(now + PAIRING_MINUTES * 60)
        return f"{code[:5]}-{code[5:]}"

    def redeem(
        self,
        data: PairingInput,
        interface: "HTTPInterface",
        *,
        device_id: str,
        platform: str | None,
    ) -> str:
        if not re.fullmatch(
            rf"{data.device_type}-[a-f0-9]{{24}}", device_id
        ):
            raise HTTPError(400, "invalid_pairing_device_id")
        if platform is not None and DEVICE_PLATFORM_TYPES.get(platform) != data.device_type:
            raise HTTPError(400, "invalid_device_platform")
        now = monotonic()
        self._check_limit(now)
        self._expire(now)
        invitation = self._codes.get(data.code.replace("-", ""))
        digest = sha256(data.token.encode("ascii")).digest()
        if invitation is None:
            self._reject(now)
        if invitation.device_id is not None:
            # A lost response may be retried with the same device-generated credential.
            if (
                invitation.digest is not None
                and invitation.installation == data.installation
                and device_id == invitation.device_id
                and compare_digest(invitation.digest, digest)
                and interface.devices.authenticate(invitation.device_id, data.token) is not None
            ):
                return invitation.device_id
            self._reject(now)
        user_id, conversation_id = interface.enrollment_scope()
        binding = HTTPDeviceBinding(
            device_id=device_id,
            device_type=data.device_type,
            display_name=(data.device_name or "").strip() or None,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        try:
            interface.devices.register(
                binding, token=data.token, installation=data.installation, platform=platform
            )
        except DeviceAlreadyRegistered as exc:
            raise HTTPError(409, "device_id_in_use") from exc
        invitation.digest = digest
        invitation.device_id = binding.device_id
        invitation.installation = data.installation
        return binding.device_id


@routes.post("/v1/devices/pair")
async def pair(request: web.Request) -> web.Response:
    interface = request.app[HTTP_INTERFACE]
    if interface.pairing is None:
        raise HTTPError(404, "pairing_unavailable")
    if not trusted_https(request):
        raise HTTPError(403, "secure_transport_required")
    data = await read_input(
        request.clone(client_max_size=4096), PairingInput, "invalid_pairing_request"
    )
    ids = request.headers.getall("X-Device-ID", [])
    platforms = request.headers.getall("X-Device-Platform", [])
    if len(ids) != 1 or len(platforms) > 1:
        raise HTTPError(400, "invalid_device_identity")
    device_id = interface.pairing.redeem(
        data, interface, device_id=ids[0], platform=next(iter(platforms), None)
    )
    return web.json_response({"device_id": device_id})
