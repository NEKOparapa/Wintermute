"""Content-addressed original files and their reusable Markdown, scoped to a conversation."""

import sqlite3
import time
from typing import TYPE_CHECKING, Literal

from pydantic import JsonValue

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore


class ChatFileStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self.db = store._connection

    def initialize(self) -> None:
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS chat_documents (user_id TEXT, conversation_id TEXT, "
            "id TEXT, raw BLOB, markdown TEXT, owner TEXT, expires REAL, "
            "PRIMARY KEY(user_id,conversation_id,id))"
        )

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

    def page(self, scope: tuple[str, str, str], offset: int, limit: int) -> tuple[str, int]:
        row = self.db.execute(
            "SELECT substr(markdown,?,?),length(markdown) FROM chat_documents "
            "WHERE user_id=? AND conversation_id=? AND id=? AND markdown IS NOT NULL",
            (offset + 1, limit, *scope),
        ).fetchone()
        if row is None:
            raise LookupError("Unknown file")
        return row[0], row[1]

    def reference(self, scope: tuple[str, str, str], name: str) -> dict[str, JsonValue]:
        excerpt, length = self.page(scope, 0, 2000)
        return {"id": scope[2], "name": name, "excerpt": excerpt, "length": length}

    def original(self, scope: tuple[str, str, str]) -> bytes:
        row = self.db.execute(
            "SELECT raw FROM chat_documents WHERE user_id=? AND conversation_id=? "
            "AND id=? AND markdown IS NOT NULL",
            scope,
        ).fetchone()
        if row is None:
            raise LookupError("Unknown file")
        return bytes(row[0])

    def list_sent(self, user: str, conversation: str, offset: int) -> list[dict[str, str]]:
        rows: list[sqlite3.Row] = self.db.execute(
            "SELECT j.value->>'$.id' AS id,j.value->>'$.name' AS name "
            "FROM conversation_events e,json_each(e.data,'$.payload.documents') j "
            "WHERE e.user_id=? AND e.conversation_id=? GROUP BY 1,2 "
            "ORDER BY MAX(e.seq) DESC,1,2 LIMIT 50 OFFSET ?",
            (user, conversation, offset),
        ).fetchall()
        return [{"id": row["id"], "name": row["name"]} for row in rows]
