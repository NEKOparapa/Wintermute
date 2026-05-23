from __future__ import annotations

import types
import unittest
from unittest.mock import Mock, patch

from openai import OpenAIError

from app.llm.llm import LLMError, OpenAICompatibleLLM


class OpenAICompatibleLLMTests(unittest.TestCase):
    def test_complete_uses_openai_client(self) -> None:
        fake_completion = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=" 正常 ")
                )
            ]
        )
        fake_client = Mock()
        fake_client.chat.completions.create.return_value = fake_completion

        with patch("app.llm.llm.OpenAI", return_value=fake_client) as openai:
            llm = OpenAICompatibleLLM(
                base_url="https://example.test/v1",
                api_key="key",
                model="model",
                timeout_seconds=3,
            )
            response = llm.complete(messages=[{"role": "user", "content": "ping"}])

        self.assertEqual(response, "正常")
        openai.assert_called_once_with(
            api_key="key",
            base_url="https://example.test/v1",
            timeout=3,
        )
        fake_client.chat.completions.create.assert_called_once_with(
            model="model",
            messages=[{"role": "user", "content": "ping"}],
        )

    def test_complete_wraps_openai_errors(self) -> None:
        fake_client = Mock()
        fake_client.chat.completions.create.side_effect = OpenAIError("bad key")

        with patch("app.llm.llm.OpenAI", return_value=fake_client):
            llm = OpenAICompatibleLLM(
                base_url="https://example.test/v1",
                api_key="key",
                model="model",
            )

            with self.assertRaises(LLMError):
                llm.complete(messages=[{"role": "user", "content": "ping"}])


if __name__ == "__main__":
    unittest.main()
