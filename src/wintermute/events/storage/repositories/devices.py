"""Persist profiles and installation bindings for authenticated HTTP devices."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore


class DeviceIdentityConflict(ValueError):
    pass


@dataclass(frozen=True)
class DeviceProfile:
    name: str | None
    platform: str | None


class DeviceStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

    def initialize(self) -> None:
        db = self._store._connection
        db.execute(
            "CREATE TABLE IF NOT EXISTS client_devices (device_id TEXT PRIMARY KEY "
            "REFERENCES devices, installation TEXT, name TEXT, platform TEXT)"
        )
        if "platform" not in {row[1] for row in db.execute("PRAGMA table_info(client_devices)")}:
            db.execute("ALTER TABLE client_devices ADD COLUMN platform TEXT")

    def observe(
        self,
        device_id: str,
        installation: str | None,
        name: str | None,
        *,
        platform: str | None = None,
        rename: bool = False,
    ) -> None:
        db = self._store._connection
        row = db.execute(
            "SELECT installation,name,platform FROM client_devices WHERE device_id=?",
            (device_id,),
        ).fetchone()
        if row is not None:
            if row["installation"] is not None and row["installation"] != installation:
                raise DeviceIdentityConflict
            name_unchanged = (
                name is None or row["name"] == name or (not rename and row["name"] is not None)
            )
            platform_unchanged = platform is None or row["platform"] == platform
            if row["installation"] == installation and name_unchanged and platform_unchanged:
                return
        with self._store._transaction():
            cursor = db.execute(
                "INSERT INTO client_devices(device_id,installation,name,platform) VALUES(?,?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET installation=excluded.installation,"
                "name=CASE WHEN ? THEN excluded.name "
                "ELSE coalesce(client_devices.name,excluded.name) END,"
                "platform=coalesce(excluded.platform,client_devices.platform) WHERE "
                "client_devices.installation IS NULL OR "
                "client_devices.installation=excluded.installation",
                (device_id, installation, name, platform, rename),
            )
            if not cursor.rowcount:
                raise DeviceIdentityConflict

    def names(self, user: str) -> dict[str, str | None]:
        return {device_id: profile.name for device_id, profile in self.profiles(user).items()}

    def profiles(self, user: str) -> dict[str, DeviceProfile]:
        rows = self._store._connection.execute(
            "SELECT d.device_id,d.name,d.platform FROM client_devices d JOIN devices t "
            "ON t.device_id=d.device_id WHERE t.user_id=? ORDER BY d.device_id",
            (user,),
        )
        return {device_id: DeviceProfile(name, platform) for device_id, name, platform in rows}
