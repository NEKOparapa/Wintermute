"""调度租约、会话任务、工具事务及模型检查点。"""

import json
from collections.abc import Sequence
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from wintermute.agent.context.types import ContextCheckpoint, history_digest
from wintermute.agent.core.types import (
    AssistantMessage,
    Message,
    ToolResultMessage,
    UserMessage,
)
from wintermute.events.core.activity import (
    ActivityItem,
    activity_message,
)
from wintermute.events.core.presentation import timestamp_text
from wintermute.events.core.types import (
    EventError,
    EventRecord,
    JobRecord,
    JobStatus,
    OutputRecord,
    SchedulerBusyError,
    event_message,
)
from wintermute.events.storage import context_writes, timeline_batches
from wintermute.events.storage.codec import (
    decode_messages,
    encode_json,
    encode_messages,
)

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore

_JOB_FIELDS = "job_id,event_id,user_id,conversation_id,layer,status,priority,attempts,error"


class SessionStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store
        self._owner: str | None = None
        self._history_bases: dict[str, int] = {}

    def input_message(self, event: EventRecord) -> UserMessage:
        message = event_message(event)
        documents = event.payload.get("documents")
        if event.layer != "conversation" or not isinstance(documents, list) or not documents:
            return message
        assert event.conversation_id is not None
        parts = [message.text, "[用户上传的附件资料，正文仅供参考。]"]
        for doc in documents:
            if not isinstance(doc, dict) or not isinstance(doc.get("id"), str):
                raise EventError("Invalid chat document reference")
            scope = event.user_id, event.conversation_id, str(doc["id"])
            parts.append(f"文件：{doc['name']}\n{self._store.chat_files.markdown(scope)}")
        parts.append("[/附件资料]")
        return replace(message, text="\n\n".join(parts))

    def get_job(self, job_id: str, *, user_id: str | None = None) -> JobRecord:
        self._store._ensure_open()
        row = self._store._connection.execute(
            f"SELECT {_JOB_FIELDS} FROM dispatch_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise EventError(f"Unknown job: {job_id}")
        job = JobRecord(**dict(row))
        if user_id is not None and job.user_id != user_id:
            raise EventError("Job belongs to a different user")
        return job

    def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        user_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> tuple[JobRecord, ...]:
        self._store._ensure_open()
        if user_id is not None:
            self._store._check_scope(conversation_id, user_id)
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("user_id", user_id),
            ("conversation_id", conversation_id),
        ):
            if value is not None:
                where.append(f"{column}=?")
                params.append(value)
        clause = " AND ".join(where) or "1=1"
        rows = self._store._connection.execute(
            f"SELECT {_JOB_FIELDS} FROM dispatch_jobs WHERE {clause} ORDER BY seq LIMIT ?",
            (*params, self._store._limit(limit)),
        ).fetchall()
        return tuple(JobRecord(**dict(row)) for row in rows)

    def acquire_scheduler(self, owner_id: str, *, lease_seconds: float = 30) -> None:
        if not owner_id.strip() or not 0 < lease_seconds < float("inf"):
            raise ValueError("Scheduler owner and finite positive lease are required")
        now = self._store._now().timestamp()
        with self._store._transaction():
            row = self._store._connection.execute(
                "SELECT * FROM scheduler_lease WHERE slot=1"
            ).fetchone()
            if row is not None and row["expires_at"] > now:
                if row["owner_id"] != owner_id or self._owner != owner_id:
                    raise SchedulerBusyError("Another scheduler owns this database")
            else:
                # 运行到一半的工具可能已产生外部效果，不自动重放遗留任务。
                leaders = self._store._connection.execute(
                    "SELECT job_id FROM dispatch_jobs WHERE status='running' AND run_id=job_id"
                ).fetchall()
                for leader in leaders:
                    self._fail_run(
                        leader["job_id"],
                        "Previous scheduler stopped before recording completion",
                        (),
                        interrupted=True,
                    )
            self._store._connection.execute(
                "INSERT INTO scheduler_lease(slot,owner_id,expires_at) VALUES(1,?,?) "
                "ON CONFLICT(slot) DO UPDATE SET owner_id=excluded.owner_id,"
                "expires_at=excluded.expires_at",
                (owner_id, now + lease_seconds),
            )
        self._owner = owner_id

    def _require_scheduler(self, owner_id: str | None = None) -> None:
        owner = owner_id or self._owner
        row = self._store._connection.execute(
            "SELECT * FROM scheduler_lease WHERE slot=1"
        ).fetchone()
        if (
            owner is None
            or self._owner != owner
            or row is None
            or row["owner_id"] != owner
            or row["expires_at"] <= self._store._now().timestamp()
        ):
            raise SchedulerBusyError("Scheduler lease is missing or expired")

    def heartbeat_scheduler(self, owner_id: str, *, lease_seconds: float = 30) -> None:
        if not 0 < lease_seconds < float("inf"):
            raise ValueError("lease_seconds must be finite and positive")
        with self._store._transaction():
            self._require_scheduler(owner_id)
            self._store._connection.execute(
                "UPDATE scheduler_lease SET expires_at=? WHERE slot=1 AND owner_id=?",
                (self._store._now().timestamp() + lease_seconds, owner_id),
            )

    def release_scheduler(self, owner_id: str) -> None:
        with self._store._transaction():
            if self._owner != owner_id:
                # 同一连接可能已经被新调度器接管，旧实例清理时不得删除新租约。
                return
            self._store._connection.execute(
                "DELETE FROM scheduler_lease WHERE owner_id=?", (owner_id,)
            )
        self._owner = None

    def claim_next(
        self,
        excluded_conversations: Sequence[str] = (),
        *,
        owner_id: str | None = None,
        defer_input: bool = False,
    ) -> JobRecord | None:
        with self._store._transaction():
            self._require_scheduler(owner_id)
            excluded = ""
            if excluded_conversations:
                placeholders = ",".join("?" for _ in excluded_conversations)
                excluded = f" AND conversation_id NOT IN ({placeholders})"
            row = self._store._connection.execute(
                "SELECT job_id,event_id,input_run_id FROM dispatch_jobs WHERE status='queued' "
                "AND conversation_id NOT IN "
                "(SELECT conversation_id FROM dispatch_jobs WHERE status='running')"
                f"{excluded} ORDER BY seq LIMIT 1",
                tuple(excluded_conversations),
            ).fetchone()
            if row is None:
                return None
            job_id = row["job_id"]
            job = self.get_job(job_id)
            self._store._connection.execute(
                "INSERT INTO session_history(conversation_id,run_id) VALUES(?,?)",
                (job.conversation_id, job_id),
            )
            if row["input_run_id"] is None and not defer_input:
                initial = self.input_message(self._store.get_event(row["event_id"]))
                self._store._connection.execute(
                    "UPDATE dispatch_jobs SET input_run_id=job_id,input_index=0,messages=? "
                    "WHERE job_id=?",
                    (encode_messages((initial,)), job_id),
                )
            self._store._connection.execute(
                "UPDATE dispatch_jobs SET status='running',attempts=attempts+1,run_id=job_id "
                "WHERE job_id=?",
                (job_id,),
            )
            return self.get_job(row["job_id"])

    def job_input_applied(self, job_id: str) -> bool:
        """该 job 的输入是否已在另一个执行中进入历史。"""
        row = self._store._connection.execute(
            "SELECT input_run_id FROM dispatch_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise EventError(f"Unknown job: {job_id}")
        return row["input_run_id"] is not None and row["input_run_id"] != job_id

    def _leader(self, run_id: str) -> JobRecord:
        row = self._store._connection.execute(
            f"SELECT {_JOB_FIELDS} FROM dispatch_jobs WHERE job_id=? AND run_id=job_id "
            "AND status='running'",
            (run_id,),
        ).fetchone()
        if row is None:
            raise EventError("Run is no longer active")
        return JobRecord(**dict(row))

    def has_pending_inputs(self, run_id: str) -> bool:
        """只有交流或通知会中断生成、阻止尚未开始的工具。"""
        row = self._store._connection.execute(
            "SELECT 1 FROM dispatch_jobs r WHERE r.job_id=? AND r.status='running' "
            "AND r.run_id=r.job_id AND EXISTS(SELECT 1 FROM dispatch_jobs j "
            "WHERE j.conversation_id=r.conversation_id AND j.status='queued' "
            "AND j.input_run_id IS NULL)",
            (run_id,),
        ).fetchone()
        return row is not None

    def _write_prefix(self, run_id: str, messages: tuple[Message, ...]) -> None:
        saved = self._store._connection.execute(
            "SELECT messages FROM dispatch_jobs WHERE job_id=?",
            (run_id,),
        ).fetchone()
        assert saved is not None
        transcript = encode_messages(messages)
        previous = json.loads(encode_messages(decode_messages(saved["messages"])))
        if json.loads(transcript)[: len(previous)] != previous:
            raise EventError("Checkpoint would replace committed history")
        self._store._connection.execute(
            "UPDATE dispatch_jobs SET messages=? WHERE job_id=?",
            (transcript, run_id),
        )

    def absorb_session_inputs(
        self,
        run_id: str,
        *,
        messages: tuple[Message, ...],
        owner_id: str | None = None,
    ) -> tuple[EventRecord | UserMessage, ...]:
        with self._store._transaction():
            self._require_scheduler(owner_id)
            leader = self._leader(run_id)
            rows = self._store._connection.execute(
                "SELECT j.job_id,j.event_id "
                "FROM dispatch_jobs j JOIN receipts r ON r.event_id=j.event_id "
                "WHERE j.conversation_id=? AND (j.status='queued' OR j.job_id=?) "
                "AND j.input_run_id IS NULL ORDER BY r.seq",
                (leader.conversation_id, run_id),
            ).fetchall()
            included: list[int] = []
            timeline = timeline_batches.pending(
                self._store._connection,
                leader.conversation_id,
                leader.user_id,
                self._store.timeline_policy,
                included,
            )
            if (timeline or rows) and self._store._connection.execute(
                "SELECT 1 FROM tool_batches WHERE run_id=?", (run_id,)
            ).fetchone():
                raise EventError("Cannot insert input into an unfinished tool batch")
            target = self._store.get_event(leader.event_id).reply_device_id
            prefix = list(messages)
            inputs: list[EventRecord | UserMessage] = []
            timeline_batches.mark_read(self._store._connection, leader.conversation_id)
            self._store._connection.execute(
                "UPDATE activity_deliveries SET applied=1,input_run_id=?,input_index=? "
                "WHERE conversation_id=? AND applied=0",
                (run_id, len(prefix) if timeline else None, leader.conversation_id),
            )
            if timeline is not None:
                written = self._store._now()
                self._store._connection.execute(
                    "INSERT INTO timeline_input_times VALUES(?,?,?)",
                    (run_id, len(prefix), written.isoformat()),
                )
                history_seq = self._store._connection.execute(
                    "SELECT seq FROM session_history WHERE run_id=?", (run_id,)
                ).fetchone()[0]
                context_writes.save(
                    self._store._connection,
                    leader.conversation_id,
                    history_seq,
                    len(prefix),
                    written,
                    timeline,
                    included,
                )
                prefix.append(timeline)
                inputs.append(timeline)
            for row in rows:
                event = self._store.get_event(row["event_id"])
                same_target = event.reply_device_id == target
                self._store._connection.execute(
                    "UPDATE dispatch_jobs SET input_run_id=?,input_index=?,run_id=?,status=?,"
                    "attempts=attempts+? WHERE job_id=?",
                    (
                        run_id,
                        len(prefix),
                        run_id if same_target else None,
                        "running" if same_target else "queued",
                        int(same_target and row["job_id"] != run_id),
                        row["job_id"],
                    ),
                )
                prefix.append(self.input_message(event))
                inputs.append(event)
            if inputs:
                self._write_prefix(run_id, tuple(prefix))
            return tuple(inputs)

    def finish_reply_attempt(
        self,
        run_id: str,
        *,
        messages: tuple[Message, ...],
        response: str,
        check_activity: bool = True,
        owner_id: str | None = None,
    ) -> Literal["completed", "input", "timeline"]:
        """在同一事务内决定提交回复，或告知 Runner 哪种新输入需要重取。"""
        if not messages or not isinstance(messages[-1], AssistantMessage):
            raise EventError("A final answer must end with an assistant message")
        if messages[-1].tool_calls or messages[-1].text != response:
            raise EventError("Final response differs from candidate message")
        with self._store._transaction():
            self._require_scheduler(owner_id)
            leader = self._leader(run_id)
            if self.has_pending_inputs(run_id):
                return "input"
            if check_activity:
                if self._store.timeline.pending_timeline(leader.conversation_id, leader.user_id):
                    return "timeline"
                timeline_batches.mark_read(self._store._connection, leader.conversation_id)
                self._store._connection.execute(
                    "UPDATE activity_deliveries SET applied=1 "
                    "WHERE conversation_id=? AND applied=0",
                    (leader.conversation_id,),
                )
            target = self._store.get_event(leader.event_id).reply_device_id
            self._finish_run(leader, messages, response, target)
            return "completed"

    def register_tool_batch(
        self,
        run_id: str,
        *,
        messages: tuple[Message, ...],
        owner_id: str | None = None,
    ) -> None:
        if (
            not messages
            or not isinstance(messages[-1], AssistantMessage)
            or not messages[-1].tool_calls
        ):
            raise EventError("Tool batch must end with a complete declaration")
        declaration = messages[-1]
        with self._store._transaction():
            self._require_scheduler(owner_id)
            self._leader(run_id)
            self._write_prefix(run_id, messages[:-1])
            self._store._connection.execute(
                "INSERT INTO tool_batches(run_id,declaration) VALUES(?,?)",
                (run_id, encode_json([asdict(declaration)])),
            )
            self._store._connection.executemany(
                "INSERT INTO tool_batch_calls(run_id,call_id,name,state) VALUES(?,?,?,'declared')",
                ((run_id, call.id, call.name) for call in declaration.tool_calls),
            )

    def try_start_tool(self, run_id: str, call_id: str, *, owner_id: str | None = None) -> bool:
        with self._store._transaction():
            self._require_scheduler(owner_id)
            self._leader(run_id)
            if self.has_pending_inputs(run_id):
                return False
            updated = self._store._connection.execute(
                "UPDATE tool_batch_calls SET state='started' WHERE run_id=? "
                "AND call_id=? AND state='declared'",
                (run_id, call_id),
            )
            if updated.rowcount != 1:
                raise EventError("Tool is unknown or has already been started")
            return True

    def finish_tool(
        self,
        run_id: str,
        result: ToolResultMessage,
        replies: tuple[str, ...] = (),
        *,
        owner_id: str | None = None,
    ) -> None:
        with self._store._transaction():
            self._require_scheduler(owner_id)
            job = self._leader(run_id)
            self._settle_tool(run_id, result)
            if replies:
                target = self._store.get_event(job.event_id).reply_device_id
                for text in replies:
                    self._append_output(job, target, text, is_final=False)
            outstanding = self._store._connection.execute(
                "SELECT 1 FROM tool_batch_calls WHERE run_id=? AND state!='settled' LIMIT 1",
                (run_id,),
            ).fetchone()
            if outstanding is None:
                self._close_tool_batch(run_id)

    def _settle_tool(self, run_id: str, result: ToolResultMessage) -> None:
        updated = self._store._connection.execute(
            "UPDATE tool_batch_calls SET state='settled',result=? WHERE run_id=? "
            "AND call_id=? AND name=? AND (state='started' OR (state='declared' AND ?))",
            (
                encode_json([asdict(result)]),
                run_id,
                result.tool_call_id,
                result.name,
                result.is_error,
            ),
        )
        if updated.rowcount != 1:
            raise EventError("Tool result does not match a pending call")

    def end_session(
        self, run_id: str, results: tuple[ToolResultMessage, ...], *, owner_id: str
    ) -> None:
        # 成功回执、剩余调用的跳过记录和任务完成状态一起提交。未接纳的输入仍然排队。
        with self._store._transaction():
            self._require_scheduler(owner_id)
            self._leader(run_id)
            for result in results:
                self._settle_tool(run_id, result)
            self._close_tool_batch(run_id)
            self._complete_run(run_id)

    def _close_tool_batch(self, run_id: str) -> bool:
        batch = self._store._connection.execute(
            "SELECT declaration FROM tool_batches WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if batch is None:
            return False
        (declaration,) = decode_messages(batch["declaration"])
        assert isinstance(declaration, AssistantMessage)
        rows = self._store._connection.execute(
            "SELECT call_id,state,result FROM tool_batch_calls WHERE run_id=?",
            (run_id,),
        ).fetchall()
        calls = {row["call_id"]: row for row in rows}
        messages: list[Message] = [declaration]
        for call in declaration.tool_calls:
            row = calls[call.id]
            if row["state"] == "settled":
                messages.extend(decode_messages(row["result"]))
            else:
                reason = (
                    "Execution outcome unknown after interruption; inspect effects before retrying"
                    if row["state"] == "started"
                    else "Tool was not started before interruption"
                )
                messages.append(ToolResultMessage(call.id, call.name, reason, True))
        self._write_prefix(run_id, (*self.get_run_messages(run_id), *messages))
        self._store._connection.execute("DELETE FROM tool_batches WHERE run_id=?", (run_id,))
        return True

    def _finish_run(
        self,
        job: JobRecord,
        messages: tuple[Message, ...],
        response: str,
        reply_device_id: str | None,
    ) -> None:
        if reply_device_id is not None:
            if self._store.get_device(reply_device_id).user_id != job.user_id:
                raise EventError("Output device belongs to a different user")
        if self._store._connection.execute(
            "SELECT 1 FROM tool_batches WHERE run_id=?", (job.job_id,)
        ).fetchone():
            raise EventError("Cannot finish with unsettled tools")
        self._write_prefix(job.job_id, messages or self.get_run_messages(job.job_id))
        self._complete_run(job.job_id)
        self._append_output(job, reply_device_id, response, is_final=True)

    def _complete_run(self, run_id: str) -> None:
        self._store._connection.execute(
            "UPDATE dispatch_jobs SET status='completed',error=NULL WHERE run_id=?",
            (run_id,),
        )
        self._history_bases.pop(run_id, None)

    def _append_output(
        self, job: JobRecord, reply_device_id: str | None, response: str, *, is_final: bool
    ) -> None:
        output = OutputRecord(
            output_id=f"out_{uuid4().hex}",
            event_id=job.event_id,
            job_id=job.job_id,
            user_id=job.user_id,
            conversation_id=job.conversation_id,
            device_id=reply_device_id,
            layer=job.layer,
            text=response,
        )
        self._store._connection.execute(
            "INSERT INTO outputs(output_id,event_id,job_id,user_id,conversation_id,device_id,"
            "layer,text,is_final) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                output.output_id,
                output.event_id,
                output.job_id,
                output.user_id,
                output.conversation_id,
                output.device_id,
                output.layer,
                output.text,
                is_final,
            ),
        )
        if response:
            self._store._connection.execute(
                "INSERT INTO dialogue_entries(user_id,conversation_id,output_id,completed_at) "
                "VALUES(?,?,?,?)",
                (
                    job.user_id,
                    job.conversation_id,
                    output.output_id,
                    timestamp_text(self._store._now()),
                ),
            )

    def fail_job(
        self,
        job_id: str,
        error: str,
        *,
        messages: tuple[Message, ...] = (),
        interrupted: bool = False,
        owner_id: str | None = None,
    ) -> None:
        with self._store._transaction():
            self._require_scheduler(owner_id)
            job = self.get_job(job_id)
            if job.status == "queued":
                self._store._connection.execute(
                    "UPDATE dispatch_jobs SET run_id=job_id WHERE job_id=?",
                    (job_id,),
                )
                self._store._connection.execute(
                    "INSERT INTO session_history(conversation_id,run_id) VALUES(?,?)",
                    (job.conversation_id, job_id),
                )
            else:
                self._leader(job_id)
            self._fail_run(job_id, error, messages, interrupted=interrupted)

    def _fail_run(
        self,
        run_id: str,
        error: str,
        messages: tuple[Message, ...],
        *,
        interrupted: bool,
    ) -> None:
        # 工具可能已产生外部效果。恢复使用持久化的逐项回执，不能覆盖为笼统的取消占位。
        if not self._close_tool_batch(run_id) and messages:
            saved = self.get_run_messages(run_id)
            if messages[: len(saved)] == saved:
                self._write_prefix(run_id, messages)
        self._store._connection.execute(
            "UPDATE dispatch_jobs SET status=?,error=? WHERE run_id=?",
            ("interrupted" if interrupted else "failed", error, run_id),
        )
        self._history_bases.pop(run_id, None)
        # 受控运行在第一次取时间线/触发消息之前中止时，仍记录原始用户输入。
        row = self._store._connection.execute(
            "SELECT event_id FROM dispatch_jobs WHERE job_id=? AND input_run_id IS NULL",
            (run_id,),
        ).fetchone()
        if row:
            prefix = self.get_run_messages(run_id)
            self._store._connection.execute(
                "UPDATE dispatch_jobs SET input_run_id=job_id,input_index=? WHERE job_id=?",
                (len(prefix), run_id),
            )
            self._write_prefix(
                run_id, (*prefix, self.input_message(self._store.get_event(row["event_id"])))
            )

    def history(
        self, conversation_id: str, user_id: str, *, exclude_run_id: str | None = None
    ) -> tuple[Message, ...]:
        """读取会话已提交的消息；装配本轮初始上下文时可排除本轮。"""
        self._store._ensure_open()
        self._store._check_scope(conversation_id, user_id)
        rows = self._store._connection.execute(
            "SELECT h.run_id,j.messages,i.data FROM session_history h "
            "LEFT JOIN dispatch_jobs j ON j.job_id=h.run_id "
            "LEFT JOIN activity_items i ON i.seq=h.activity_seq "
            "WHERE h.conversation_id=? AND (? IS NULL OR h.run_id IS NOT ?) ORDER BY h.seq",
            (conversation_id, exclude_run_id, exclude_run_id),
        ).fetchall()
        history: list[Message] = []
        for row in rows:
            if row["data"] is None:
                for index, msg in enumerate(decode_messages(row["messages"])):
                    # Old transcripts stored a preview. Resolve only an actual attachment
                    # event at this input position, never an ID found in user-written text.
                    if isinstance(msg, UserMessage) and "excerpt 是开头摘录" in msg.text:
                        event = self._store._connection.execute(
                            "SELECT e.data FROM dispatch_jobs j JOIN conversation_events e "
                            "ON e.event_id=j.event_id WHERE j.input_run_id=? AND j.input_index=? "
                            "AND e.user_id=? AND e.conversation_id=? "
                            "AND json_array_length(e.data,'$.payload.documents')>0",
                            (row["run_id"], index, user_id, conversation_id),
                        ).fetchone()
                        if event is not None:
                            msg = self.input_message(EventRecord.model_validate_json(event[0]))
                    history.append(msg)
            else:
                history.append(activity_message(ActivityItem.model_validate_json(row["data"])))
        return tuple(history)

    def get_run_messages(self, job_id: str, *, user_id: str | None = None) -> tuple[Message, ...]:
        """由任一关联 job 查询本次实际执行的唯一消息序列。"""
        self.get_job(job_id, user_id=user_id)
        row = self._store._connection.execute(
            "SELECT COALESCE(r.messages,'[]') AS messages FROM dispatch_jobs j "
            "LEFT JOIN dispatch_jobs r ON j.run_id=r.job_id WHERE j.job_id=?",
            (job_id,),
        ).fetchone()
        assert row is not None
        return decode_messages(row["messages"])

    def load_model_read_count(self, conversation_id: str, user_id: str) -> int:
        """返回该会话已由主模型确认的连续原始消息数。"""
        self._store._check_scope(conversation_id, user_id)
        row = self._store._connection.execute(
            "SELECT read_count FROM session_model_state WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        return int(row["read_count"]) if row is not None else 0

    def load_context_checkpoint(
        self, conversation_id: str, user_id: str
    ) -> ContextCheckpoint | None:
        self._store._check_scope(conversation_id, user_id)
        row = self._store._connection.execute(
            "SELECT summary,summarized_messages,prefix_digest,compactions "
            "FROM session_model_state WHERE conversation_id=? AND summarized_messages>0",
            (conversation_id,),
        ).fetchone()
        return ContextCheckpoint(**dict(row)) if row is not None else None

    def record_model_read(
        self,
        run_id: str,
        read_count: int,
        checkpoint: ContextCheckpoint | None,
        *,
        messages: tuple[Message, ...],
        owner_id: str | None = None,
    ) -> None:
        """仅由普通推理完成时调用。当前运行的原文、已读位置和新摘要一起提交。"""
        if type(read_count) is not int or read_count < 0:
            raise EventError("Model read count must be a nonnegative integer")
        with self._store._transaction():
            self._require_scheduler(owner_id)
            leader = self._leader(run_id)
            self._write_prefix(run_id, messages)
            # 同会话只有一个活跃运行。运行之前的历史长度固定，工具循环只需数当前前缀。
            if run_id not in self._history_bases:
                self._history_bases[run_id] = self._store._connection.execute(
                    "SELECT COALESCE(SUM(COALESCE(json_array_length(j.messages),1)),0) "
                    "FROM session_history h LEFT JOIN dispatch_jobs j ON j.job_id=h.run_id "
                    "WHERE h.conversation_id=? AND h.seq<(SELECT seq FROM session_history "
                    "WHERE run_id=?)",
                    (leader.conversation_id, run_id),
                ).fetchone()[0]
            if read_count > self._history_bases[run_id] + len(messages):
                raise EventError("Model read count exceeds stored conversation history")
            self._store._connection.execute(
                "INSERT INTO session_model_state(conversation_id,read_count) VALUES(?,?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET read_count=excluded.read_count "
                "WHERE excluded.read_count>read_count",
                (leader.conversation_id, read_count),
            )
            if checkpoint is None:
                return
            cut = checkpoint.summarized_messages
            if cut > read_count:
                return
            previous = self._store._connection.execute(
                "SELECT summarized_messages,compactions,read_count FROM session_model_state "
                "WHERE conversation_id=?",
                (leader.conversation_id,),
            ).fetchone()
            if read_count < previous["read_count"] or (
                cut,
                checkpoint.compactions,
            ) <= (previous["summarized_messages"], previous["compactions"]):
                return
            # transform_context 可能改变 SDK 侧消息顺序和内容。只保存能在会话原文上重建的摘要。
            history = self.history(leader.conversation_id, leader.user_id)
            if cut > len(history) or history_digest(history[:cut]) != checkpoint.prefix_digest:
                return
            self._store._connection.execute(
                "UPDATE session_model_state SET summary=?,summarized_messages=?,"
                "prefix_digest=?,compactions=? WHERE conversation_id=?",
                (
                    checkpoint.summary,
                    cut,
                    checkpoint.prefix_digest,
                    checkpoint.compactions,
                    leader.conversation_id,
                ),
            )
