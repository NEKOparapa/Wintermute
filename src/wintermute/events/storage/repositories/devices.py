"""Persist profiles and installation bindings for authenticated HTTP devices."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from wintermute.config.models import HTTPDeviceBinding
from wintermute.events.core import Device, EventError

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore


class DeviceIdentityConflict(ValueError):
    pass


@dataclass(frozen=True)
class DeviceProfile:
    name: str | None
    platform: str | None


@dataclass(frozen=True)
class HTTPCredential:
    # A null binding overrides a settings-file device. A null digest revokes either kind.
    binding: HTTPDeviceBinding | None
    digest: bytes | None


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
        db.execute(
            "CREATE TABLE IF NOT EXISTS http_device_credentials (device_id TEXT PRIMARY KEY "
            "REFERENCES devices, binding TEXT, digest BLOB)"
        )

    def http_credentials(self, device_id: str | None = None) -> dict[str, HTTPCredential]:
        sql = "SELECT device_id,binding,digest FROM http_device_credentials"
        rows = (
            self._store._connection.execute(sql)
            if device_id is None
            else self._store._connection.execute(sql + " WHERE device_id=?", (device_id,))
        )
        return {
            row["device_id"]: HTTPCredential(
                HTTPDeviceBinding.model_validate_json(row["binding"]) if row["binding"] else None,
                row["digest"],
            )
            for row in rows
        }

    def register_http(self, binding: HTTPDeviceBinding, device: Device, digest: bytes) -> None:
        db = self._store._connection
        with self._store._transaction():
            if db.execute(
                "SELECT 1 FROM devices WHERE device_id=?", (device.device_id,)
            ).fetchone():
                raise EventError("Device ID is already registered")
            self._store._register_conversation(binding.conversation_id, binding.user_id)
            self._store._register_device(device)
            db.execute(
                "INSERT INTO http_device_credentials(device_id,binding,digest) VALUES(?,?,?)",
                (device.device_id, binding.model_dump_json(), digest),
            )

    def set_http_digest(self, device_id: str, digest: bytes | None) -> None:
        with self._store._transaction():
            self._store._connection.execute(
                "INSERT INTO http_device_credentials(device_id,digest) VALUES(?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET digest=excluded.digest",
                (device_id, digest),
            )

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
