"""桌面帧上传、识别队列和识别结果持久化。"""

from typing import TYPE_CHECKING

from pydantic import JsonValue

from wintermute.events.core.activity import (
    desktop_visual_activity,
)
from wintermute.events.core.types import (
    EventRecord,
)

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore


class DesktopStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

    def begin_frame_upload(
        self, user: str, terminal: str, producer_event: str, fingerprint: str
    ) -> bool:
        return (
            self._store._connection.execute(
                "INSERT OR IGNORE INTO desktop_frame_uploads VALUES(?,?,?,?)",
                (user, terminal, producer_event, fingerprint),
            ).rowcount
            == 1
        )

    def pending_frame_uploads(self) -> list[tuple[str, str, str, str]]:
        return [
            (row[0], row[1], row[2], row[3])
            for row in self._store._connection.execute(
                "SELECT user_id,terminal_id,producer_event_id,fingerprint "
                "FROM desktop_frame_uploads"
            )
        ]

    def finish_frame_upload(self, terminal: str, producer_event: str, fingerprint: str) -> None:
        self._store._connection.execute(
            "DELETE FROM desktop_frame_uploads WHERE terminal_id=? AND producer_event_id=? "
            "AND fingerprint=?",
            (terminal, producer_event, fingerprint),
        )

    def next_desktop_frame(self) -> tuple[EventRecord, int, int] | None:
        row = self._store._connection.execute(
            "SELECT event_id,frame_index,attempts FROM desktop_vision_queue WHERE retry_at<=? "
            "ORDER BY rowid LIMIT 1",
            (self._store._now().timestamp(),),
        ).fetchone()
        return (self._store.get_event(row[0]), row[1], row[2]) if row else None

    def retry_desktop_frame(self, event_id: str, index: int, delay: float) -> None:
        with self._store._transaction():
            if self._store._connection.execute(
                "UPDATE desktop_vision_queue SET attempts=attempts+1,retry_at=? "
                "WHERE event_id=? AND frame_index=?",
                (self._store._now().timestamp() + delay, event_id, index),
            ).rowcount:
                self._store._connection.execute(
                    "UPDATE perception_events SET data=json_set(data,'$.payload.image_recognition',"
                    "'retrying') WHERE event_id=?",
                    (event_id,),
                )

    def record_desktop_frame(self, event_id: str, index: int, text: str) -> None:
        """Attach the description to its original visit. Publish visual evidence once per visit."""
        with self._store._transaction():
            if not self._store._connection.execute(
                "DELETE FROM desktop_vision_queue WHERE event_id=? AND frame_index=?",
                (event_id, index),
            ).rowcount:
                return
            event = self._store.get_event(event_id)
            value, frames = event.payload["value"], event.payload["frames"]
            assert isinstance(value, dict) and isinstance(frames, list)
            frame = frames[index]
            assert isinstance(frame, dict)
            note: dict[str, JsonValue] = {
                "frame_index": index,
                "occurred_at": frame["occurred_at"],
                "text": text,
            }
            if frame.get("window_title"):
                note["window_title"] = frame["window_title"]
            notes = value["visual_observations"]
            assert isinstance(notes, list)
            # Retries may finish out of order. Frame indices follow the validated capture times.
            for pos, previous in enumerate(notes):
                assert isinstance(previous, dict) and isinstance(previous["frame_index"], int)
                if previous["frame_index"] > index:
                    notes.insert(pos, note)
                    break
            else:
                notes.append(note)
            remaining, failed = self._store._connection.execute(
                "SELECT COUNT(*),MAX(attempts) FROM desktop_vision_queue WHERE event_id=?",
                (event_id,),
            ).fetchone()
            event.payload["image_recognition"] = (
                "retrying" if failed else "pending" if remaining else "ready"
            )
            self._store._connection.execute(
                "UPDATE perception_events SET data=? WHERE event_id=?",
                (event.model_dump_json(), event_id),
            )
            if not remaining:
                # The dwell is already available to the running Agent. Its delayed supplement
                # contains only the new observations and cannot add the dwell duration twice.
                self._store.timeline._project_activity(
                    desktop_visual_activity(event, self._store._now())
                )
