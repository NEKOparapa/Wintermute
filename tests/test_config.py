from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config.config import Settings


class SettingsTests(unittest.TestCase):
    def test_loads_basic_fields_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            data_dir = Path(tmp) / "custom-data"
            log_dir = Path(tmp) / "custom-logs"
            path.write_text(
                json.dumps(
                    {
                        "data_dir": str(data_dir),
                        "log_dir": str(log_dir),
                        "host": "0.0.0.0",
                        "port": 9000,
                        "base_url": "https://example.test/v1",
                        "api_key": "key",
                        "model": "model",
                    }
                ),
                encoding="utf-8",
            )

            settings = Settings.load(path)

            self.assertEqual(settings.data_dir, data_dir)
            self.assertEqual(settings.log_dir, log_dir)
            self.assertTrue(data_dir.is_dir())
            self.assertTrue(log_dir.is_dir())
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 9000)
        self.assertEqual(settings.base_url, "https://example.test/v1")
        self.assertEqual(settings.api_key, "key")
        self.assertEqual(settings.model, "model")

    def test_memory_thresholds_default_when_omitted(self) -> None:
        """配置文件不写记忆相关字段时,使用合理默认值。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "data_dir": str(Path(tmp) / "d"),
                        "log_dir": str(Path(tmp) / "l"),
                    }
                ),
                encoding="utf-8",
            )
            settings = Settings.load(path)

        self.assertEqual(settings.recent_rounds, 5)
        self.assertEqual(settings.session_compress_trigger_tokens, 4000)
        self.assertEqual(settings.prompt_budget_session_tokens, 8000)
        self.assertEqual(settings.prompt_budget_daily_tokens, 8000)
        self.assertEqual(settings.prompt_budget_weekly_tokens, 4000)
        self.assertEqual(settings.prompt_budget_monthly_tokens, 2000)
        self.assertEqual(settings.daily_rollup_hour, 3)
        self.assertEqual(settings.daily_rollup_minute, 0)
        self.assertEqual(settings.weekly_rollup_weekday, 0)
        self.assertEqual(settings.weekly_rollup_hour, 3)
        self.assertEqual(settings.weekly_rollup_minute, 30)
        self.assertEqual(settings.monthly_rollup_day, 1)
        self.assertEqual(settings.monthly_rollup_hour, 4)
        self.assertEqual(settings.monthly_rollup_minute, 0)

    def test_memory_thresholds_overridden_by_config(self) -> None:
        """JSON 配置文件能覆盖记忆相关的所有字段。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "data_dir": str(Path(tmp) / "d"),
                        "log_dir": str(Path(tmp) / "l"),
                        "recent_rounds": 8,
                        "session_compress_trigger_tokens": 6000,
                        "prompt_budget_session_tokens": 12000,
                        "prompt_budget_daily_tokens": 10000,
                        "prompt_budget_weekly_tokens": 5000,
                        "prompt_budget_monthly_tokens": 3000,
                        "daily_rollup_hour": 2,
                        "daily_rollup_minute": 30,
                        "weekly_rollup_weekday": 6,
                        "weekly_rollup_hour": 5,
                        "weekly_rollup_minute": 15,
                        "monthly_rollup_day": 2,
                        "monthly_rollup_hour": 5,
                        "monthly_rollup_minute": 45,
                    }
                ),
                encoding="utf-8",
            )
            settings = Settings.load(path)

        self.assertEqual(settings.recent_rounds, 8)
        self.assertEqual(settings.session_compress_trigger_tokens, 6000)
        self.assertEqual(settings.prompt_budget_session_tokens, 12000)
        self.assertEqual(settings.prompt_budget_daily_tokens, 10000)
        self.assertEqual(settings.prompt_budget_weekly_tokens, 5000)
        self.assertEqual(settings.prompt_budget_monthly_tokens, 3000)
        self.assertEqual(settings.daily_rollup_hour, 2)
        self.assertEqual(settings.daily_rollup_minute, 30)
        self.assertEqual(settings.weekly_rollup_weekday, 6)
        self.assertEqual(settings.weekly_rollup_hour, 5)
        self.assertEqual(settings.weekly_rollup_minute, 15)
        self.assertEqual(settings.monthly_rollup_day, 2)
        self.assertEqual(settings.monthly_rollup_hour, 5)
        self.assertEqual(settings.monthly_rollup_minute, 45)


if __name__ == "__main__":
    unittest.main()
