"""Opt-in snapshots use only the vault's own local repository."""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from wintermute.memory import MemoryVault


@unittest.skipUnless(shutil.which("git"), "Local git executable is unavailable")
class MemorySnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def git(self, directory: Path, *arguments: str, check: bool = True) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(directory),
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(10):
            output, error = await process.communicate()
        if check:
            self.assertEqual(process.returncode, 0, error.decode("utf-8", errors="replace"))
        return output.decode("utf-8", errors="replace").strip()

    async def test_local_snapshot_isolation_idempotency_and_disabled_signing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            await self.git(parent, "init")
            (parent / "outside.md").write_text("Parent repository data", encoding="utf-8")
            await self.git(parent, "add", "--", "outside.md")

            vault = MemoryVault(parent / "memory", git_snapshot_enabled=True)
            await vault.write(
                "create",
                "note/project.md",
                actor="human_api",
                reason="Create project memory",
                content="# Project\nFirst version\n",
            )
            (vault.root / ".trash" / "backup.md").write_text("Retained backup", encoding="utf-8")
            await vault.snapshot("Initial local snapshot")

            actual_root = Path(await self.git(vault.root, "rev-parse", "--show-toplevel"))
            self.assertEqual(actual_root, vault.root)
            self.assertEqual(await self.git(vault.root, "rev-list", "--count", "HEAD"), "1")
            tracked = (await self.git(vault.root, "ls-files")).splitlines()
            self.assertIn("note/project.md", tracked)
            self.assertTrue(any(path.startswith("log/") for path in tracked))
            self.assertFalse(any(path.startswith(".trash/") for path in tracked))
            self.assertNotIn("outside.md", tracked)
            self.assertEqual(
                await self.git(parent, "diff", "--cached", "--name-only"), "outside.md"
            )
            self.assertEqual(await self.git(parent, "log", "--format=%H", check=False), "")

            original_head = await self.git(vault.root, "rev-parse", "HEAD")
            await vault.snapshot("Unchanged snapshot")
            self.assertEqual(await self.git(vault.root, "rev-parse", "HEAD"), original_head)
            self.assertEqual(await self.git(vault.root, "rev-list", "--count", "HEAD"), "1")

            # A user's normal signed-commit configuration must not prompt or block automation.
            await self.git(vault.root, "config", "commit.gpgSign", "true")
            await self.git(vault.root, "config", "gpg.program", "missing-memory-test-gpg")
            await vault.write(
                "insert",
                "note/project.md",
                actor="main_agent",
                reason="Update project memory",
                content="Second version\n",
            )
            await vault.snapshot("Subsequent local snapshot")
            self.assertEqual(await self.git(vault.root, "rev-list", "--count", "HEAD"), "2")
            self.assertIn(
                "Second version", await self.git(vault.root, "show", "HEAD:note/project.md")
            )

    async def test_disabled_snapshots_do_not_create_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = MemoryVault(Path(temporary) / "memory")
            await vault.snapshot("Disabled")
            self.assertFalse((vault.root / ".git").exists())
