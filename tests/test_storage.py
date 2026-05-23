from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.storage.storage import GlobalEventStore


class GlobalEventStoreTests(unittest.TestCase):
    def test_appends_events_to_single_json_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GlobalEventStore(Path(tmp))
            store.append_event(
                source="user",
                type="user_message",
                content="你好",
            )
            store.append_event(
                source="assistant",
                type="assistant_response",
                content="你好。",
            )

            events = store.load_events()
            raw = json.loads((Path(tmp) / "events.json").read_text(encoding="utf-8"))

        self.assertEqual([event["content"] for event in events], ["你好", "你好。"])
        self.assertIsInstance(raw, list)
        self.assertEqual(raw[0]["type"], "user_message")
        self.assertEqual(raw[1]["type"], "assistant_response")
        self.assertIn("id", raw[0])
        self.assertIn("timestamp", raw[0])


if __name__ == "__main__":
    unittest.main()
