"""Track the presence and capabilities advertised by authenticated devices."""

from collections.abc import Sequence
from time import monotonic
from typing import Annotated

from pydantic import Field

type ToolName = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,63}$")]
_FRESH_SECONDS = 15


class DeviceCapabilities:
    """Authenticated traffic indicates presence; only claims renew advertised tools."""

    def __init__(self) -> None:
        self._last_seen: dict[str, float] = {}
        self._advertised: dict[str, tuple[float, tuple[str, ...]]] = {}

    def observe(self, device_id: str) -> None:
        self._last_seen[device_id] = monotonic()

    def forget(self, device_id: str) -> None:
        self._last_seen.pop(device_id, None)
        self._advertised.pop(device_id, None)

    def update(self, device_id: str, tools: Sequence[str]) -> None:
        now = monotonic()
        self._last_seen[device_id] = now
        if tools:
            self._advertised[device_id] = (now, tuple(tools))
        else:
            self._advertised.pop(device_id, None)

    def tools(self, device_id: str) -> tuple[str, ...]:
        advertised = self._advertised.get(device_id)
        if advertised and monotonic() - advertised[0] <= _FRESH_SECONDS:
            return advertised[1]
        return ()

    def presence(self, device_id: str) -> tuple[str, int | None]:
        seen = self._last_seen.get(device_id)
        if seen is None:
            return "unknown", None
        age = monotonic() - seen
        return ("online" if age <= _FRESH_SECONDS else "offline"), int(age)
