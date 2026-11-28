"""Content-addressed original files and their reusable Markdown, scoped to a conversation."""

import time
from typing import TYPE_CHECKING, Literal

from pydantic import JsonValue

if TYPE_CHECKING:
    from purin.events.storage.sqlite import SQLiteStore


class ChatFileStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self.db = store._connection

    def claim(self, scope: tuple[str, str, str], owner: str) -> Literal["ready", "busy", "new"]:
        now = time.time()
        result = self.db.execute(
            "INSERT INTO chat_documents(user_id,conversation_id,id,owner,expires) "
            "VALUES(?,?,?,?,?) ON CONFLICT(user_id,conversation_id,id) DO UPDATE SET "
            "owner=excluded.owner,expires=excluded.expires "
            "WHERE markdown IS NULL AND expires < ?",
            (*scope, owner, now + 300, now),
        )
        if result.rowcount:
            return "new"
        row = self.db.execute(
            "SELECT markdown IS NOT NULL FROM chat_documents "
            "WHERE user_id=? AND conversation_id=? AND id=?",
            scope,
        ).fetchone()
        return "ready" if row and row[0] else "busy"

    def finish(self, scope: tuple[str, str, str], owner: str, data: bytes, markdown: str) -> None:
        result = self.db.execute(
            "UPDATE chat_documents SET raw=?,markdown=?,owner=NULL,expires=NULL "
            "WHERE user_id=? AND conversation_id=? AND id=? AND owner=?",
            (data, markdown, *scope, owner),
        )
        if not result.rowcount:
            raise RuntimeError("Chat document conversion lease expired")

    def release(self, scope: tuple[str, str, str], owner: str) -> None:
        self.db.execute(
            "DELETE FROM chat_documents WHERE user_id=? AND conversation_id=? "
            "AND id=? AND owner=? AND markdown IS NULL",
            (*scope, owner),
        )

    def reference(self, scope: tuple[str, str, str], name: str) -> dict[str, JsonValue]:
        # Keep the compact event payload stable: retries compare its original receipt hash.
        row = self.db.execute(
            "SELECT substr(markdown,1,2000),length(markdown) FROM chat_documents "
            "WHERE user_id=? AND conversation_id=? AND id=? AND markdown IS NOT NULL",
            scope,
        ).fetchone()
        if row is None:
            raise LookupError("Unknown file")
        excerpt, length = row
        return {"id": scope[2], "name": name, "excerpt": excerpt, "length": length}

    def markdown(self, scope: tuple[str, str, str]) -> str:
        row = self.db.execute(
            "SELECT markdown FROM chat_documents WHERE user_id=? AND conversation_id=? AND id=? "
            "AND markdown IS NOT NULL",
            scope,
        ).fetchone()
        if row is None:
            raise LookupError("Unknown chat document")
        return str(row[0])

    def original(self, scope: tuple[str, str, str]) -> bytes:
        row = self.db.execute(
            "SELECT raw FROM chat_documents WHERE user_id=? AND conversation_id=? "
            "AND id=? AND markdown IS NOT NULL",
            scope,
        ).fetchone()
        if row is None:
            raise LookupError("Unknown file")
        return bytes(row[0])
