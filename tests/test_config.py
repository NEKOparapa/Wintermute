from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config.config import Settings


class SettingsTests(unittest.TestCase):
    def test_loads_host_and_port_from_config(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
