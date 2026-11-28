"""Create the event database in its current format."""

import sqlite3
from importlib.resources import files
from typing import TYPE_CHECKING

from purin.events.core.types import EventError

if TYPE_CHECKING:
    from purin.events.storage.sqlite import SQLiteStore


SCHEMA_VERSION = 41


class SchemaManager:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

    def initialize(self) -> None:
        db = self._store._connection
        with self._store._transaction():
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version or db.execute(
                "SELECT 1 FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' LIMIT 1"
            ).fetchone():
                raise EventError(
                    f"Unsupported database version {version}, expected {SCHEMA_VERSION}"
                )
            schema = files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
            statement = ""
            for line in schema.splitlines(keepends=True):
                statement += line
                if line.rstrip().endswith(";") and sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise EventError("Incomplete database schema")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
