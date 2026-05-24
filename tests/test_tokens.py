from __future__ import annotations

import unittest

from app.memory.tokens import TokenCounter


class TokenCounterTests(unittest.TestCase):
    def test_count_text_returns_positive_for_non_empty(self) -> None:
        counter = TokenCounter()
        self.assertEqual(counter.count_text(""), 0)
        self.assertGreater(counter.count_text("hello world"), 0)
        self.assertGreater(counter.count_text("中文测试"), 0)

    def test_count_messages_includes_per_message_overhead(self) -> None:
        counter = TokenCounter()
        single = counter.count_messages([{"role": "user", "content": "hello"}])
        double = counter.count_messages(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
            ]
        )
        self.assertGreater(double, single * 2 - 1)
        self.assertGreater(single, counter.count_text("hello"))

    def test_unknown_model_falls_back_to_default_encoding(self) -> None:
        # 不应该抛错，且能给出大于 0 的 token 数。
        counter = TokenCounter(model="some-private-model-not-in-tiktoken")
        self.assertGreater(counter.count_text("ping"), 0)


if __name__ == "__main__":
    unittest.main()
