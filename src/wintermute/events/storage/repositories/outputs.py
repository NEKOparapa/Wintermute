"""回复输出队列及投递状态。"""

from typing import TYPE_CHECKING, Any

from wintermute.events.core.types import (
    EventError,
    OutputRecord,
)

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore

_OUTPUT_FIELDS = (
    "output_id,event_id,job_id,user_id,conversation_id,device_id,layer,text,status,error"
)


class OutputStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store

    def get_output(self, output_id: str, *, user_id: str | None = None) -> OutputRecord:
        self._store._ensure_open()
        row = self._store._connection.execute(
            f"SELECT {_OUTPUT_FIELDS} FROM outputs WHERE output_id=?", (output_id,)
        ).fetchone()
        if row is None:
            raise EventError(f"Unknown output: {output_id}")
        output = OutputRecord(**dict(row))
        if user_id is not None and output.user_id != user_id:
            raise EventError("Output belongs to a different user")
        return output

    def list_outputs(
        self,
        *,
        user_id: str | None = None,
        conversation_id: str | None = None,
        status: str | None = None,
        device_id: str | None = None,
        job_id: str | None = None,
        limit: int = 100,
    ) -> tuple[OutputRecord, ...]:
        self._store._ensure_open()
        if user_id is not None:
            self._store._check_scope(conversation_id, user_id)
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("user_id", user_id),
            ("conversation_id", conversation_id),
            ("status", status),
            ("device_id", device_id),
        ):
            if value is not None:
                where.append(f"{column}=?")
                params.append(value)
        if job_id is not None:
            where.append("job_id=(SELECT run_id FROM dispatch_jobs WHERE job_id=?)")
            params.append(job_id)
        clause = " AND ".join(where) or "1=1"
        rows = self._store._connection.execute(
            f"SELECT {_OUTPUT_FIELDS} FROM outputs WHERE {clause} ORDER BY seq LIMIT ?",
            (*params, self._store._limit(limit)),
        ).fetchall()
        return tuple(OutputRecord(**dict(row)) for row in rows)

    def pending_outputs(
        self, user_id: str | None = None, *, device_id: str | None = None
    ) -> tuple[OutputRecord, ...]:
        return self.list_outputs(
            user_id=user_id, status="pending", device_id=device_id, limit=10000
        )

    def mark_output_delivered(self, output_id: str) -> None:
        with self._store._transaction():
            self.get_output(output_id)
            self._store._connection.execute(
                "UPDATE outputs SET status='delivered',error=NULL WHERE output_id=?", (output_id,)
            )

    def mark_output_failed(self, output_id: str, error: str) -> None:
        with self._store._transaction():
            self.get_output(output_id)
            self._store._connection.execute(
                "UPDATE outputs SET error=? WHERE output_id=? AND status='pending'",
                (error, output_id),
            )
