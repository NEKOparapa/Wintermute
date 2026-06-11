from __future__ import annotations

import json
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError


class WintermuteAPIError(RuntimeError):
    """Wintermute event API request failed."""


class WintermuteClient:
    """Client for Wintermute POST /event."""

    def __init__(self, event_url: str, *, timeout_seconds: int = 300) -> None:
        self.event_url = event_url
        self.timeout_seconds = timeout_seconds

    def send_event(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            self.event_url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except (HTTPError, URLError) as exc:
            raise WintermuteAPIError("Wintermute /event 请求失败。") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WintermuteAPIError("Wintermute /event 响应不是 JSON。") from exc
        if not isinstance(data, dict):
            raise WintermuteAPIError("Wintermute /event 响应必须是 JSON 对象。")

        message = data.get("message")
        if not isinstance(message, str) or not message.strip():
            raise WintermuteAPIError("Wintermute /event 响应缺少 message。")
        return message
