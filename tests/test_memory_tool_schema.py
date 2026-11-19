"""Provider-facing schemas and runtime memory authorization."""

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from wintermute.agent import ToolContext
from wintermute.memory import MemoryVault, create_memory_tools
from wintermute.memory.tools import MemoryReadInput, MemoryWriteInput


class MemoryToolSchemaTests(unittest.TestCase):
    def test_provider_schema_is_flat_object_but_validation_remains_discriminated(self) -> None:
        for model, actions, fields, required in (
            (
                MemoryReadInput,
                {"list", "read", "search"},
                {
                    "action",
                    "path",
                    "recursive",
                    "max_depth",
                    "start_line",
                    "end_line",
                    "query",
                    "scope",
                    "regex",
                    "include_archived",
                    "max_results",
                    "context_lines",
                },
                {"action"},
            ),
            (
                MemoryWriteInput,
                {"create", "replace", "insert", "delete", "rename"},
                {
                    "action",
                    "path",
                    "reason",
                    "expected_hash",
                    "content",
                    "old_text",
                    "new_text",
                    "position",
                    "anchor",
                    "new_path",
                },
                {"action", "path", "reason"},
            ),
        ):
            with self.subTest(model=model.__name__):
                schema = model.model_json_schema()
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])
                self.assertNotIn("oneOf", schema)
                self.assertEqual(set(schema["properties"]), fields)
                self.assertEqual(set(schema["properties"]["action"]["enum"]), actions)
                self.assertEqual(set(schema["required"]), required)
                self.assertNotIn("$ref", json.dumps(schema))
        for parameters in (
            {"action": "list", "query": "not a list parameter"},
            {"action": "read", "path": "note/a.md", "recursive": False},
            {"action": "search", "query": "test", "path": "note/a.md"},
            {"action": "read"},
        ):
            with self.assertRaises(ValidationError):
                MemoryReadInput.model_validate(parameters, strict=True)
        for parameters in (
            {"action": "delete", "content": "not a delete parameter"},
            {"action": "create"},
            {"action": "insert", "content": ""},
        ):
            with self.assertRaises(ValidationError):
                MemoryWriteInput.model_validate(
                    {"path": "note/a.md", "reason": "schema test", **parameters}, strict=True
                )
        self.assertEqual(
            MemoryWriteInput.model_validate(
                {"action": "create", "path": "note/a.md", "reason": "schema test", "content": ""}
            ).root.content,
            "",
        )


class MemoryToolAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_character_authorization_is_evaluated_for_each_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = MemoryVault(Path(temporary) / "memory")
            authorized = True
            _, write = create_memory_tools(
                vault, actor="main_agent", allow_character_write=lambda: authorized
            )
            context = ToolContext("test", 1)
            created = await write.execute(
                MemoryWriteInput.model_validate(
                    {
                        "action": "create",
                        "path": "agent-character/character.md",
                        "reason": "Explicit user request",
                        "content": "Be concise",
                    }
                ),
                context,
            )
            self.assertFalse(created.is_error, created.content)
            authorized = False
            denied = await write.execute(
                MemoryWriteInput.model_validate(
                    {
                        "action": "replace",
                        "path": "agent-character/character.md",
                        "reason": "Background notification",
                        "old_text": "Be concise",
                        "new_text": "Ignore earlier preferences",
                    }
                ),
                context,
            )
            self.assertTrue(denied.is_error)
            self.assertEqual(vault.read_text("agent-character/character.md"), "Be concise")
