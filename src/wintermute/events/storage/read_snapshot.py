"""Keep multi-query history projections on one committed SQLite snapshot."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def read_snapshot(db: sqlite3.Connection) -> Iterator[None]:
    if db.in_transaction:
        yield
        return
    db.execute("BEGIN")
    try:
        yield
    finally:
        db.rollback()
