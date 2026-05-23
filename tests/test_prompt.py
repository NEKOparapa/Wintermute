from __future__ import annotations

import unittest

from app.prompt.prompt import SYSTEM_PROMPT, build_messages


class PromptTests(unittest.TestCase):
    def test_build_messages_converts_history_events(self) -> None:
        messages = build_messages(
            [
                {"type": "user_message", "content": "你好"},
                {"type": "assistant_response", "content": "你好。"},
            ]
        )

        self.assertEqual(messages[0], {"role": "system", "content": SYSTEM_PROMPT})
        self.assertEqual(messages[1], {"role": "user", "content": "你好"})
        self.assertEqual(messages[2], {"role": "assistant", "content": "你好。"})


if __name__ == "__main__":
    unittest.main()
