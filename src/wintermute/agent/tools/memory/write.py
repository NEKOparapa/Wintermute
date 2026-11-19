"""Apply authorized changes to a user's memory vault."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from wintermute.agent import Tool, ToolContext, ToolResult

from .parameters import CreateMemory, InsertMemory, MemoryWriteInput, RenameMemory, ReplaceMemory

if TYPE_CHECKING:
    from wintermute.memory.security import MemoryActor
    from wintermute.memory.vault import MemoryVault


def create_memory_write_tool(
    vault: "MemoryVault",
    *,
    actor: "MemoryActor",
    channel: str = "conversation",
    allow_character_write: bool | Callable[[], bool] = False,
) -> Tool[MemoryWriteInput]:
    async def write(arguments: MemoryWriteInput, context: ToolContext) -> ToolResult:
        arg = arguments.root
        try:
            character_authorized = (
                allow_character_write()
                if callable(allow_character_write)
                else allow_character_write
            )
            result = await vault.write(
                arg.action,
                arg.path,
                actor=actor,
                reason=arg.reason,
                channel=channel,
                allow_character_write=character_authorized,
                expected_hash=arg.expected_hash,
                content=arg.content if isinstance(arg, CreateMemory | InsertMemory) else "",
                old_text=arg.old_text if isinstance(arg, ReplaceMemory) else "",
                new_text=arg.new_text if isinstance(arg, ReplaceMemory) else "",
                position=arg.position if isinstance(arg, InsertMemory) else "append",
                anchor=arg.anchor if isinstance(arg, InsertMemory) else None,
                new_path=arg.new_path if isinstance(arg, RenameMemory) else None,
            )
            return ToolResult(json.dumps(result, ensure_ascii=False))
        except (OSError, ValueError) as exc:
            return ToolResult(f"{type(exc).__name__}: {exc}", is_error=True)

    return Tool(
        "memory_write",
        "Create, uniquely replace, insert, soft-delete or rename a Markdown "
        "memory file. A reason is required; expected_hash prevents stale writes. First read "
        "related notes. Change agent-character only on the user's current explicit request. "
        "Never write credentials or log/.",
        MemoryWriteInput,
        write,
    )
