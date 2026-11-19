"""Whole-vault search deadlines must bound work after the calling tool is cancelled."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from wintermute.memory.regex_search import RegexMatcher
from wintermute.memory.vault import MemoryVault, TextMemorySearcher


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class SearchDeadlineTests(unittest.TestCase):
    def test_cumulative_scan_time_stops_even_when_each_document_is_below_limit(self) -> None:
        clock = Clock()
        vault = MagicMock(spec=MemoryVault)
        vault.zone = ZoneInfo("UTC")
        vault.list_files.return_value = [
            {"is_dir": False, "path": f"note/{index}.md"} for index in range(20)
        ]
        matcher = MagicMock()

        def read(path: str) -> str:
            clock.now += 0.8
            return "ordinary text"

        vault.read_text.side_effect = read
        matcher.__enter__.return_value = matcher
        matcher.matches.return_value = []
        with patch("wintermute.memory.vault.monotonic", clock), patch(
            "wintermute.memory.vault.RegexMatcher", return_value=matcher
        ), self.assertRaisesRegex(ValueError, "5 second time limit"):
            TextMemorySearcher().search(
                vault, "unmatched", scope="note", regex=True, include_archived=True,
                max_results=20, context_lines=2,
            )
        self.assertLess(vault.read_text.call_count, 20)
        remaining = [call.kwargs["timeout_seconds"] for call in matcher.matches.call_args_list]
        self.assertAlmostEqual(remaining[-1], 0.2)
        matcher.__exit__.assert_called_once()

    def test_literal_search_also_has_an_overall_deadline(self) -> None:
        clock = Clock()
        vault = MagicMock(spec=MemoryVault)
        vault.zone = ZoneInfo("UTC")
        vault.list_files.return_value = [
            {"is_dir": False, "path": f"note/{index}.md"} for index in range(3)
        ]

        def read(path: str) -> str:
            clock.now += 3
            return "ordinary text"

        vault.read_text.side_effect = read
        with patch("wintermute.memory.vault.monotonic", clock), self.assertRaisesRegex(
            ValueError, "5 second time limit"
        ):
            TextMemorySearcher().search(
                vault, "unmatched", scope="note", regex=False, include_archived=True,
                max_results=20, context_lines=2,
            )
        self.assertEqual(vault.read_text.call_count, 2)

    def test_directory_enumeration_checks_the_search_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = MemoryVault(Path(directory) / "memory")
            vault.initialize()
            (vault.root / "note/a.md").write_text("text", encoding="utf-8")
            with patch("wintermute.memory.vault.monotonic", return_value=6), (
                self.assertRaisesRegex(ValueError, "5 second time limit")
            ):
                vault.list_files("note", _deadline=5)

    def test_regex_timer_uses_the_smaller_remaining_budget(self) -> None:
        matcher = object.__new__(RegexMatcher)
        matcher.query = "pattern"
        matcher.timeout_seconds = 1
        matcher.process = MagicMock()
        matcher.process.stdout.readline.return_value = '{"lines": []}\n'
        with patch("wintermute.memory.regex_search.threading.Timer") as timer:
            self.assertEqual(matcher.matches("text", 20, timeout_seconds=0.25), [])
            self.assertEqual(timer.call_args.args[0], 0.25)
            timer.return_value.cancel.assert_called_once()
            timer.return_value.join.assert_called_once()


if __name__ == "__main__":
    unittest.main()
