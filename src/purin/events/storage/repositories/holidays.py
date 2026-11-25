"""Persistent cache of published holiday-cn annual bulletins, shared by all conversations."""

import sqlite3


class HolidayYearStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def initialize(self) -> None:
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS holiday_cn_years (year INTEGER PRIMARY KEY, "
            "checked_at REAL NOT NULL, document BLOB NOT NULL)"
        )

    def get(self, year: int) -> tuple[float, bytes] | None:
        row = self._db.execute(
            "SELECT checked_at, document FROM holiday_cn_years WHERE year=?", (year,)
        ).fetchone()
        return (row[0], row[1]) if row is not None else None

    def save(self, year: int, checked_at: float, document: bytes) -> None:
        self._db.execute(
            "INSERT INTO holiday_cn_years(year,checked_at,document) VALUES (?,?,?) "
            "ON CONFLICT(year) DO UPDATE SET "
            "checked_at=excluded.checked_at, document=excluded.document "
            "WHERE excluded.checked_at >= holiday_cn_years.checked_at",
            (year, checked_at, document),
        )
