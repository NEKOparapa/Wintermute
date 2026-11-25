"""用户通知、投递确认和通知附件。"""

from datetime import datetime
from typing import TYPE_CHECKING

from purin.events.storage import notifications, schedules

if TYPE_CHECKING:
    from purin.events.storage.sqlite import SQLiteStore


class NotificationStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

    def previous_user_notification(
        self, *, user_id: str, conversation_id: str, request_key: str, fingerprint: str
    ) -> str | None:
        self._store._ensure_open()
        return notifications.previous(
            self._store._connection, user_id, conversation_id, request_key, fingerprint
        )

    def send_user_notification(
        self,
        *,
        user_id: str,
        conversation_id: str,
        targets: tuple[str, ...],
        title: str,
        body: str,
        expires_at: datetime | None,
        files: list[notifications.FileSnapshot],
        request_key: str,
        fingerprint: str,
    ) -> str:
        with self._store._transaction():
            self._store._check_scope(conversation_id, user_id)
            for target in targets:
                if self._store.get_device(target).user_id != user_id or target.startswith(
                    "purin:internal:"
                ):
                    raise ValueError("接收设备无效")
            return notifications.insert(
                self._store._connection,
                user_id,
                conversation_id,
                targets,
                title,
                body,
                schedules.stamp(self._store._now()),
                schedules.stamp(expires_at) if expires_at else None,
                files,
                request_key,
                fingerprint,
            )

    def list_user_notifications(
        self,
        *,
        user_id: str,
        conversation_id: str,
        device_id: str,
        cursor: str | None = None,
        limit: int = 50,
        unread_only: bool = False,
        pending_only: bool = False,
    ) -> tuple[list[notifications.NotificationItem], str | None]:
        self._store._ensure_open()
        return notifications.page(
            self._store._connection,
            (device_id, user_id, conversation_id),
            schedules.stamp(self._store._now()),
            limit,
            cursor,
            unread_only,
            pending_only,
        )

    def ack_user_notification(
        self,
        *,
        user_id: str,
        conversation_id: str,
        device_id: str,
        notification_id: str,
        read: bool = False,
    ) -> notifications.NotificationItem:
        with self._store._transaction():
            return notifications.acknowledge(
                self._store._connection,
                (device_id, user_id, conversation_id),
                notification_id,
                schedules.stamp(self._store._now()),
                read,
            )

    def notification_file(
        self,
        *,
        user_id: str,
        conversation_id: str,
        device_id: str,
        notification_id: str,
        file_id: str,
    ) -> tuple[notifications.AttachmentInfo, int]:
        self._store._ensure_open()
        return notifications.file(
            self._store._connection,
            (device_id, user_id, conversation_id),
            notification_id,
            file_id,
        )

    def read_notification_chunk(self, rowid: int, offset: int, size: int) -> bytes:
        self._store._ensure_open()
        if size < 0 or size > 1024 * 1024:
            raise ValueError("Invalid file chunk size")
        with self._store._connection.blobopen(
            "notification_files", "data", rowid, readonly=True
        ) as blob:
            blob.seek(offset)
            return blob.read(size)
