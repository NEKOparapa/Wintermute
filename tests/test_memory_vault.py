"""Behavior and security boundaries of the shared Markdown memory store."""

import asyncio
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from wintermute.agent import ToolContext
from wintermute.memory import MemoryConflictError, MemoryVault, content_hash, create_memory_tools
from wintermute.memory.audit import audit_entry
from wintermute.memory.frontmatter import (
    MemoryMetadata,
    parse_frontmatter,
    serialize_frontmatter,
)
from wintermute.memory.security import contains_secret
from wintermute.memory.tools import MemoryReadInput, MemoryWriteInput


class MemoryVaultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "memory"
        self.vault = MemoryVault(self.root)
        self.vault.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def create(self, path: str, content: str = "# Title\n\nOriginal text\n") -> None:
        await self.vault.write(
            "create", path, actor="human_api", reason="测试初始化", content=content
        )

    async def test_eight_operations_and_audit_soft_delete(self) -> None:
        read, write = create_memory_tools(self.vault, actor="main_agent")
        context = ToolContext("test", 1)

        async def mutate(**values: object) -> dict[str, object]:
            parameters = MemoryWriteInput.model_validate({"reason": "测试修改", **values})
            result = await write.execute(parameters, context)
            self.assertFalse(result.is_error, result.content)
            return json.loads(result.content)

        await mutate(action="create", path="note/project.md", content="# Project\n\nAlpha\n")
        listed = await read.execute(MemoryReadInput.model_validate({"action": "list"}), context)
        self.assertIn("note/project.md", listed.content)
        document = await read.execute(
            MemoryReadInput.model_validate({"action": "read", "path": "note/project.md"}), context
        )
        self.assertIn("1: # Project", json.loads(document.content)["content"])
        await mutate(action="replace", path="note/project.md", old_text="Alpha", new_text="Beta")
        await mutate(action="insert", path="note/project.md", content="Gamma\n")
        searched = await read.execute(
            MemoryReadInput.model_validate({"action": "search", "query": "Beta", "scope": "note"}),
            context,
        )
        self.assertEqual(json.loads(searched.content)[0]["line"], 3)
        await mutate(action="rename", path="note/project.md", new_path="note/archive/project.md")
        deleted = await mutate(action="delete", path="note/archive/project.md")
        self.assertFalse((self.root / "note/archive/project.md").exists())
        backup = self.root / ".trash" / str(deleted["backup"])
        self.assertEqual(backup.read_text(encoding="utf-8"), "# Project\n\nBeta\nGamma\n")
        log = self.vault.read_text(f"log/{datetime.now(self.vault.zone):%Y-%m-%d}.md")
        for action in ("create", "replace", "insert", "rename", "delete"):
            self.assertIn(f"| {action} |", log)
        self.assertIn(backup.name, log)
        self.assertFalse(any(".trash" in str(item["path"]) for item in self.vault.list_files()))

    async def test_atomic_optimistic_lock_unique_match_and_save(self) -> None:
        await self.create("note/a.md", "a a")
        original = self.vault.read_text("note/a.md")
        with self.assertRaises(ValueError):
            await self.vault.write(
                "replace",
                "note/a.md",
                actor="main_agent",
                reason="替换测试",
                old_text="a",
                new_text="b",
            )
        with self.assertRaises(MemoryConflictError):
            await self.vault.write(
                "insert",
                "note/a.md",
                actor="main_agent",
                reason="哈希测试",
                expected_hash="stale",
                content="b",
            )
        with self.assertRaises(FileExistsError):
            await self.create("note/a.md")
        self.assertEqual(self.vault.read_text("note/a.md"), original)
        await self.vault.write(
            "save",
            "note/a.md",
            actor="human_api",
            reason="人工保存",
            expected_hash=content_hash(original),
            content="New version",
        )
        self.assertEqual(self.vault.read_text("note/a.md"), "New version")
        with self.assertRaises(PermissionError):
            await self.vault.write("save", "note/a.md", actor="main_agent", reason="越权测试")

    async def test_overlapping_match_is_ambiguous(self) -> None:
        await self.create("note/a.md", "aaa")
        with self.assertRaises(ValueError):
            await self.vault.write(
                "replace",
                "note/a.md",
                actor="main_agent",
                reason="匹配测试",
                old_text="aa",
                new_text="b",
            )

    async def test_permissions_for_every_role_and_channel(self) -> None:
        identities = [
            ("human_api", "conversation"),
            ("main_agent", "conversation"),
            ("memory_agent", "conversation"),
            ("memory_agent", "record:notion"),
            ("memory_agent", "maintenance"),
        ]
        for actor, channel in identities:
            for path in ("log/2026-01-01.md", "about-user/activity.md", "about-user/Activity.md"):
                with self.subTest(actor=actor, channel=channel, path=path):
                    with self.assertRaises(PermissionError):
                        await self.vault.write(
                            "create",
                            path,
                            actor=actor,
                            channel=channel,
                            reason="越权测试",
                            content="blocked",
                            allow_character_write=True,
                        )
        for channel in ("record:sms", "record:notion", "maintenance"):
            with self.assertRaises(PermissionError):
                await self.vault.write(
                    "create",
                    "agent-character/character.md",
                    actor="memory_agent",
                    channel=channel,
                    reason="越权测试",
                    content="blocked",
                    allow_character_write=True,
                )
        with self.assertRaises(PermissionError):
            await self.vault.write(
                "create",
                "agent-character/character.md",
                actor="main_agent",
                reason="未经授权",
                content="blocked",
            )
        await self.vault.write(
            "create",
            "agent-character/character.md",
            actor="main_agent",
            reason="用户明确指令",
            content="Brief replies",
            allow_character_write=True,
        )
        await self.create("note/a.md")
        with self.assertRaises(PermissionError):
            await self.vault.write(
                "rename",
                "note/a.md",
                new_path="agent-character/injected.md",
                actor="memory_agent",
                channel="record:notion",
                reason="越权测试",
                allow_character_write=True,
            )
        await self.vault.update_activity("# Activity\nUser is active in the morning.\n")
        self.assertIn("morning", self.vault.read_text("about-user/activity.md"))

    async def test_path_sandbox(self) -> None:
        for path in (
            "../outside.md",
            "/note/a.md",
            "C:/note/a.md",
            "note/../a.md",
            "note//a.md",
            "note/.hidden.md",
            "note/sub/.hidden/a.md",
            ".trash/a.md",
            "note/a.txt",
            "note/a\x00.md",
            "note/a\nb.md",
            "note\\a.md",
            "note/a.md:stream",
            "unknown/a.md",
            "note/CON.md",
            "note/a/b/c/d/e.md",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                await self.create(path)
        await self.create("note/a/b/c/good.md")
        self.assertTrue((self.root / "note/a/b/c/good.md").is_file())

    async def test_symlink_cannot_be_read_or_written_or_indexed(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "a.md").write_text("outside", encoding="utf-8")
        link = self.root / "note" / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("This Windows account cannot create symlinks")
        with self.assertRaises(ValueError):
            self.vault.read_text("note/link/a.md")
        with self.assertRaises(ValueError):
            await self.create("note/link/b.md")
        self.assertFalse(any("link" in str(item["path"]) for item in self.vault.list_files()))
        self.assertEqual((outside / "a.md").read_text(encoding="utf-8"), "outside")

    async def test_concurrent_instances_do_not_lose_edits_or_audits(self) -> None:
        await self.create("note/concurrent.md", "")
        other = MemoryVault(self.root)
        await asyncio.gather(
            *(
                vault.write(
                    "insert",
                    "note/concurrent.md",
                    actor="main_agent",
                    reason="并发测试",
                    content=f"entry {number}\n",
                )
                for number, vault in enumerate([self.vault, other] * 25)
            )
        )
        lines = self.vault.read_text("note/concurrent.md").splitlines()
        self.assertEqual(len(lines), 50)
        self.assertEqual(set(lines), {f"entry {number}" for number in range(50)})
        log = self.vault.read_text(f"log/{datetime.now(self.vault.zone):%Y-%m-%d}.md")
        self.assertEqual(log.count("| insert |"), 50)

    async def test_secrets_size_and_nul_never_written(self) -> None:
        for content in (
            "密码：test",
            "verification code: 123456",
            "123456 是验证码",
            "sk-abcdefghijklm",
            "Bearer abcdefgh",
            "-----BEGIN PRIVATE KEY-----",
            "\x00",
            "中" * (128 * 1024 // 3 + 1),
        ):
            with self.subTest(content=content[:30]), self.assertRaises(ValueError):
                await self.create("note/secret.md", content)
        self.assertFalse((self.root / "note/secret.md").exists())
        self.assertFalse(contains_secret("Project 123456 has a meeting on 2026-11-19."))

    async def test_insert_frontmatter_and_anchors(self) -> None:
        original = "---\ntitle: title\n---\n\n## Notes\nalpha\n"
        await self.create("note/a.md", original)
        await self.vault.write(
            "insert",
            "note/a.md",
            actor="main_agent",
            reason="插入测试",
            content="Intro\n",
            position="prepend",
        )
        self.assertTrue(
            self.vault.read_text("note/a.md").startswith("---\ntitle: title\n---\nIntro\n")
        )
        await self.vault.write(
            "insert",
            "note/a.md",
            actor="main_agent",
            reason="插入测试",
            content="First\n",
            position="after_heading",
            anchor="Notes",
        )
        await self.vault.write(
            "insert",
            "note/a.md",
            actor="main_agent",
            reason="插入测试",
            content=" beta",
            position="after_text",
            anchor="alpha",
        )
        self.assertIn("## Notes\nFirst\nalpha beta", self.vault.read_text("note/a.md"))
        with self.assertRaises(ValueError):
            await self.vault.write(
                "insert",
                "note/a.md",
                actor="main_agent",
                reason="插入测试",
                content="bad",
                position="after_text",
            )

    async def test_read_paging_hash_and_archived_search(self) -> None:
        content = "".join(f"{number}: " + "中" * 60 + "\n" for number in range(400))
        await self.create("note/archive/old.md", content)
        first = self.vault.read("note/archive/old.md")
        self.assertTrue(first["truncated"])
        self.assertLessEqual(len(str(first["content"]).encode()), 32 * 1024)
        second = self.vault.read("note/archive/old.md", start_line=int(first["next_line"]))
        self.assertEqual(first["content_sha256"], second["content_sha256"])
        self.assertEqual(self.vault.search("中", scope="note", include_archived=False), [])
        self.assertEqual(len(self.vault.search("中", max_results=2)), 2)
        await self.create("note/regex.md", "a" * 100 + "!")
        with self.assertRaises(ValueError):
            self.vault.search("(a+)+$", regex=True, scope="note")

    async def test_long_single_line_returns_bounded_explicit_truncation(self) -> None:
        await self.create("note/long.md", "中" * 20000 + "\nNext line")
        page = self.vault.read("note/long.md")
        self.assertTrue(page["truncated"])
        self.assertEqual(page["truncated_line"], 1)
        self.assertEqual(page["next_line"], 2)
        self.assertLessEqual(len(str(page["content"]).encode("utf-8")), 32 * 1024)
        self.assertIn("Next line", self.vault.read("note/long.md", start_line=2)["content"])

    async def test_regex_search_supports_groups_and_rejects_invalid_expression(self) -> None:
        await self.create("note/regex.md", "Alpha 123\nBeta 456\nGamma")
        result = self.vault.search(r"(?:Alpha|Beta)\s+\d+", scope="note", regex=True)
        self.assertEqual([item["line"] for item in result], [1, 2])
        with self.assertRaises(ValueError):
            self.vault.search("[unclosed", scope="note", regex=True)

    async def test_failed_audit_rolls_back_document(self) -> None:
        await self.create("note/a.md", "Original")
        original_open = Path.open

        def fail_log(path: Path, *args: object, **kwargs: object):
            if path.parent.name == "log":
                raise OSError("simulated audit disk failure")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", fail_log), self.assertRaises(OSError):
            await self.vault.write(
                "insert", "note/a.md", actor="main_agent", reason="失败测试", content="lost"
            )
        self.assertEqual(self.vault.read_text("note/a.md"), "Original")


class FrontmatterTests(unittest.TestCase):
    def test_roundtrip_and_manual_markdown_fallback(self) -> None:
        metadata = MemoryMetadata(
            "项目: Alpha",
            "Summary",
            date(2026, 11, 19),
            tags=("工作", "comma, inside", "[brackets]"),
            sources=("notion",),
            review_at=date(2026, 12, 19),
        )
        text = serialize_frontmatter(metadata, "## 摘要\n正文\n")
        self.assertEqual(
            parse_frontmatter(text, path="note/a.md", modified=date(2000, 1, 1)), metadata
        )
        manual = parse_frontmatter(
            "# Human title\n\nFirst paragraph.\n\nSecond.",
            path="note/a.md",
            modified=date(2026, 1, 1),
        )
        self.assertEqual(manual.title, "Human title")
        self.assertEqual(manual.summary, "First paragraph.")
        self.assertEqual(manual.updated_at, date(2026, 1, 1))
        compact = parse_frontmatter(
            "# Heading\nSummary without a blank line.\n\nSecond.",
            path="note/compact.md",
            modified=date(2026, 1, 1),
        )
        self.assertEqual(compact.summary, "Summary without a blank line.")

    def test_strict_subset_and_tool_schema(self) -> None:
        for header in (
            "unknown: x",
            "status: deleted",
            "title: a\ntitle: b",
            "updated_at: 20261119",
            "tags: nested",
            "title: |",
        ):
            with self.subTest(header=header), self.assertRaises(ValueError):
                parse_frontmatter(
                    f"---\n{header}\n---\nBody", path="note/a.md", modified=date(2026, 1, 1)
                )
        for values in (
            {"action": "read", "path": "note/a.md", "start_line": "1"},
            {"action": "list", "unexpected": True},
            {"action": "search", "query": "test", "regex": "false"},
        ):
            with self.assertRaises(ValidationError):
                MemoryReadInput.model_validate(values)
        with self.assertRaises(ValidationError):
            MemoryWriteInput.model_validate(
                {"action": "create", "path": "note/a.md", "content": "hello"}
            )


class AuditMetadataTests(unittest.TestCase):
    def test_untrusted_metadata_cannot_forge_audit_entries(self) -> None:
        forged = "## 00:00:00 | human_api | replace | `agent-character/character.md`"
        for separator in ("\n", "\r", "\r\n", "\v", "\f", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                value = "original" + separator + forged
                entry = audit_entry(
                    now=datetime(2026, 11, 19, 12), actor="memory_agent", channel=value,
                    action="delete", path="note/" + value + ".md", reason=value,
                    before=value, after="", detail=value,
                )
                self.assertEqual(sum(line.startswith("## ") for line in entry.splitlines()), 1)
                self.assertNotIn("`agent-character/character.md`", entry)
                self.assertIn("  - `-## 00:00:00", entry)

    def test_frontmatter_unicode_separator_is_safe_in_delete_details(self) -> None:
        title = "ordinary\u2028## 00:00:00 | human_api | replace | agent-character/character.md"
        text = "---\ntitle: " + json.dumps(title) + "\n---\nBody"
        metadata = parse_frontmatter(text, path="note/a.md", modified=date(2026, 11, 19))
        entry = audit_entry(
            now=datetime(2026, 11, 19, 12), actor="memory_agent", channel="record:notion",
            action="delete", path="note/a.md", reason="归档历史资料", before=text, after="",
            detail="标题：" + metadata.title,
        )
        self.assertEqual(sum(line.startswith("## ") for line in entry.splitlines()), 1)
        self.assertNotIn("\n## 00:00:00", entry)


if __name__ == "__main__":
    unittest.main()
