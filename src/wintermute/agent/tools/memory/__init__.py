"""Memory tools shared by the main agent and the background memory worker."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from wintermute.agent import Tool

from .parameters import (
    CreateMemory,
    DeleteMemory,
    InsertMemory,
    ListMemory,
    MemoryReadInput,
    MemoryWriteInput,
    ReadMemory,
    RenameMemory,
    ReplaceMemory,
    SearchMemory,
)
from .read import create_memory_read_tool
from .write import create_memory_write_tool

if TYPE_CHECKING:
    from wintermute.memory.security import MemoryActor
    from wintermute.memory.vault import MemoryVault

__all__ = [
    "CreateMemory",
    "DeleteMemory",
    "InsertMemory",
    "ListMemory",
    "MemoryReadInput",
    "MemoryWriteInput",
    "ReadMemory",
    "RenameMemory",
    "ReplaceMemory",
    "SearchMemory",
    "create_memory_tools",
]


def create_memory_tools(
    vault: "MemoryVault",
    *,
    actor: "MemoryActor",
    channel: str = "conversation",
    allow_character_write: bool | Callable[[], bool] = False,
) -> tuple[Tool[Any], ...]:
    return (
        create_memory_read_tool(vault),
        create_memory_write_tool(
            vault, actor=actor, channel=channel, allow_character_write=allow_character_write
        ),
    )
