from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.app import WintermuteService
from app.storage.storage import GlobalEventStore


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, *, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.responses.pop(0)


class WintermuteServiceTests(unittest.TestCase):
    def test_handles_message_and_uses_full_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = FakeLLM(["第一条回复", "第二条回复"])
            service = WintermuteService(GlobalEventStore(Path(tmp)), llm)

            first = service.handle_message("第一条")
            second = service.handle_message("第二条")
            events = service.store.load_events()

        self.assertEqual(first.message, "第一条回复")
        self.assertEqual(second.message, "第二条回复")
        self.assertEqual([event["content"] for event in events], ["第一条", "第一条回复", "第二条", "第二条回复"])
        self.assertEqual(
            [(item["role"], item["content"]) for item in llm.calls[-1]],
            [
                ("system", llm.calls[-1][0]["content"]),
                ("user", "第一条"),
                ("assistant", "第一条回复"),
                ("user", "第二条"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
