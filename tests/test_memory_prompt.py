"""Prompt ordering, index retention and independent byte budgets."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from wintermute.memory import MemoryVault, build_memory_prompt, format_index, scan_index
from wintermute.memory.prompt import DefaultPromptBudgets


class MemoryPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = MemoryVault(Path(self.temporary.name) / "memory")
        self.vault.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def put(self, path: str, content: str) -> None:
        file = self.vault.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")

    def test_sections_note_index_and_archive_expiration(self) -> None:
        self.put("agent-character/character.md", "Be concise.")
        self.put("about-user/profile.md", "Likes Python.")
        self.put("note/z.md", "---\nupdated_at: 2026-11-18\n---\n# Last\n\nSummary Z.")
        self.put("note/a.md", "# First\n\nSummary A.")
        self.put("note/archive/old.md", "# Old")
        self.put("note/archived.md", "---\nstatus: archived\n---\n# Archived")
        self.put("note/expired.md", "---\nexpires_at: 2026-11-18\n---\n# Expired")
        self.put("note/.hidden.md", "# Hidden")
        self.put("note/.hidden/x.md", "# Hidden")
        entries = scan_index(self.vault, today=date(2026, 11, 19))
        self.assertEqual([entry.path for entry in entries], ["note/a.md", "note/z.md"])
        prompt = build_memory_prompt(self.vault, today=date(2026, 11, 19))
        self.assertLess(prompt.text.index("## agent-character"), prompt.text.index("## about-user"))
        self.assertLess(prompt.text.index("## about-user"), prompt.text.index("## note 索引"))
        self.assertLess(prompt.text.index("## note 索引"), prompt.text.index("## 记忆使用指南"))
        self.assertIn("Likes Python", prompt.text)
        self.assertFalse(prompt.needs_compaction)

    def test_byte_budget_truncation_and_newest_retention_stable_order(self) -> None:
        for path, updated in (("a", "2026-01-01"), ("b", "2026-11-18"), ("c", "2026-11-19")):
            self.put(f"note/{path}.md", f"---\nupdated_at: {updated}\n---\n# {path}\n\nSummary")
        entries = scan_index(self.vault)
        index = format_index(entries, max_entries=2)
        self.assertNotIn("note/a.md", index)
        self.assertLess(index.index("note/b.md"), index.index("note/c.md"))
        self.assertIn("已省略 1", index)
        limited = format_index(entries, max_bytes=150)
        self.assertLessEqual(len(limited.encode("utf-8")), 150)
        self.put("about-user/profile.md", "中" * 1000)
        budget = DefaultPromptBudgets(
            max_character_bytes=128,
            max_about_user_bytes=512,
            max_index_bytes=150,
            max_index_entries=2,
        )
        prompt = build_memory_prompt(self.vault, budget)
        self.assertTrue(prompt.needs_compaction)
        about = prompt.text.partition("## about-user\n")[2].partition("\n## note 索引")[0]
        self.assertLessEqual(len(about.encode("utf-8")), 512)
        self.assertIn("记忆已截断", about)

    def test_malformed_header_and_secrets_do_not_break_or_leak_prompt(self) -> None:
        self.put("note/bad.md", "---\ncomplex: {nested: yes}\n---\nBody")
        self.put("about-user/profile.md", "# Profile\n\npassword: bad-value\nLikes Python")
        prompt = build_memory_prompt(self.vault)
        self.assertIn("note/bad.md", prompt.text)
        self.assertIn("Likes Python", prompt.text)
        self.assertNotIn("bad-value", prompt.text)

    def test_expired_about_user_data_and_optional_guidelines(self) -> None:
        self.put("about-user/travel.md", "---\nexpires_at: 2026-11-18\n---\nOld trip")
        self.put(
            "about-user/profile.md",
            "Stable fact\n"
            "Old state [expires_at: 2026-11-18]\n"
            "Current state [expires_at: 2026-11-19]\n"
            "Invalid date stays [expires_at: 2026-99-99]",
        )
        prompt = build_memory_prompt(self.vault, today=date(2026, 11, 19), include_guidelines=False)
        self.assertNotIn("Old trip", prompt.text)
        self.assertNotIn("Old state", prompt.text)
        self.assertIn("Stable fact", prompt.text)
        self.assertIn("Current state", prompt.text)
        self.assertIn("Invalid date stays", prompt.text)
        self.assertNotIn("## 记忆使用指南", prompt.text)
        self.assertTrue(prompt.text.endswith("[/长期记忆参考资料]"))


if __name__ == "__main__":
    unittest.main()
