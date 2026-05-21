from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def complete(self, *, system: str, user: str) -> str:
        pass


@dataclass
class OpenAICompatibleLLM:
    base_url: str
    api_key: str | None
    model: str | None
    timeout_seconds: int = 60

    def complete(self, *, system: str, user: str) -> str:
        if not self.api_key or not self.model:
            raise LLMError(
                "LLM 未配置。请在 config/settings.json 中设置 api_key 和 model，"
                "或设置 WINTERMUTE_API_KEY 和 WINTERMUTE_MODEL。"
            )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.4,
        }
        url = self.base_url.rstrip("/") + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise LLMError(f"LLM 请求失败: {exc}") from exc

        try:
            data = json.loads(raw)
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError("LLM 响应不符合 chat completions 格式。") from exc
