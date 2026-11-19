"""Persist names and installation bindings for authenticated HTTP devices."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore


class DeviceIdentityConflict(ValueError):
    pass


class DeviceStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

    def initialize(self) -> None:
        self._store._connection.execute(
            "CREATE TABLE IF NOT EXISTS client_devices (device_id TEXT PRIMARY KEY "
            "REFERENCES devices, installation TEXT, name TEXT)"
        )

    def observe(
        self,
        device_id: str,
        installation: str | None,
        name: str | None,
        *,
        rename: bool = False,
    ) -> None:
        db = self._store._connection
        row = db.execute(
            "SELECT installation,name FROM client_devices WHERE device_id=?", (device_id,)
        ).fetchone()
        if row is not None:
            if row["installation"] is not None and row["installation"] != installation:
                raise DeviceIdentityConflict
            if row["installation"] == installation and (
                name is None or row["name"] == name or (not rename and row["name"] is not None)
            ):
                return
        with self._store._transaction():
            cursor = db.execute(
                "INSERT INTO client_devices(device_id,installation,name) VALUES(?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET installation=excluded.installation,"
                "name=CASE WHEN ? THEN excluded.name "
                "ELSE coalesce(client_devices.name,excluded.name) END WHERE "
                "client_devices.installation IS NULL OR "
                "client_devices.installation=excluded.installation",
                (device_id, installation, name, rename),
            )
            if not cursor.rowcount:
                raise DeviceIdentityConflict

    def names(self, user: str) -> dict[str, str | None]:
        rows = self._store._connection.execute(
            "SELECT d.device_id,d.name FROM client_devices d JOIN devices t "
            "ON t.device_id=d.device_id WHERE t.user_id=? ORDER BY d.device_id",
            (user,),
        )
        return {row[0]: row[1] for row in rows}
