from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app.app import WintermuteService
from app.http_api import build_http_server
from app.storage.storage import GlobalEventStore


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, *, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return "收到。"


class HttpApiTests(unittest.TestCase):
    def test_health_and_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = FakeLLM()
            service = WintermuteService(GlobalEventStore(Path(tmp)), llm)
            server = build_http_server(service, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            try:
                health = self._get_json(f"{base_url}/health")
                chat = self._post_json(
                    f"{base_url}/chat",
                    {"message": "你好"},
                )
                events = service.store.load_events()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(health, {"status": "ok"})
        self.assertEqual(chat, {"message": "收到。"})
        self.assertEqual([event["content"] for event in events], ["你好", "收到。"])
        self.assertEqual(llm.calls[0][-1], {"role": "user", "content": "你好"})

    def _get_json(self, url: str) -> dict[str, object]:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
