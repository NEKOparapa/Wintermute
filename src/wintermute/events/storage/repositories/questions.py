"""Durable async user questions that inject answers into the notification layer."""

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import AwareDatetime, JsonValue

from wintermute.events.core.types import Device, EventError, EventInput, Identifier, _Record

if TYPE_CHECKING:
    from wintermute.events.storage.sqlite import SQLiteStore

type QuestionStatus = Literal["pending", "answered", "dismissed"]

MAX_OPTIONS = 6
MAX_TITLE_LEN = 120
MAX_QUESTION_LEN = 2000
MAX_OPTION_LEN = 120
MAX_ANSWER_LEN = 4000


class QuestionConflict(ValueError):
    """A question creation or resolution conflicts with stored state."""


class QuestionItem(_Record):
    id: Identifier
    title: str
    question: str
    options: list[str]
    multi_select: bool
    allow_custom: bool
    status: QuestionStatus
    created_at: AwareDatetime
    answered_at: AwareDatetime | None
    answered_device_id: str | None
    selected_options: list[str]
    answer_text: str


def initialize(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS user_questions ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, "
        "user_id TEXT NOT NULL, conversation_id TEXT NOT NULL REFERENCES conversations, "
        "run_id TEXT NOT NULL, call_id TEXT NOT NULL, "
        "reply_device_id TEXT REFERENCES devices, "
        "title TEXT NOT NULL, question TEXT NOT NULL, options TEXT NOT NULL, "
        "multi_select INTEGER NOT NULL DEFAULT 0, allow_custom INTEGER NOT NULL DEFAULT 1, "
        "status TEXT NOT NULL, created_at REAL NOT NULL, answered_at REAL, "
        "answered_device_id TEXT REFERENCES devices, "
        "selected_options TEXT NOT NULL DEFAULT '[]', answer_text TEXT NOT NULL DEFAULT '', "
        "UNIQUE(user_id,conversation_id,run_id,call_id))"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS user_questions_pending ON user_questions "
        "(user_id,conversation_id,seq DESC) WHERE status='pending'"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS user_questions_history ON user_questions "
        "(user_id,conversation_id,seq DESC)"
    )


def _record(row: sqlite3.Row) -> QuestionItem:
    return QuestionItem.model_validate(
        {
            "id": row["id"],
            "title": row["title"],
            "question": row["question"],
            "options": json.loads(row["options"]),
            "multi_select": bool(row["multi_select"]),
            "allow_custom": bool(row["allow_custom"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "answered_at": row["answered_at"],
            "answered_device_id": row["answered_device_id"],
            "selected_options": json.loads(row["selected_options"]),
            "answer_text": row["answer_text"],
        }
    )


def _origin(user_id: str) -> Device:
    return Device(
        device_id="wintermute:internal:question:"
        + hashlib.sha256(user_id.encode()).hexdigest(),
        user_id=user_id,
        allowed_layers=("notification",),
    )


def _notice(
    row: sqlite3.Row,
    now: datetime,
    status: Literal["answered", "dismissed"],
    text: str | None = None,
) -> EventInput:
    selected: list[JsonValue] = list(json.loads(row["selected_options"]))
    if text is None:
        if status == "answered":
            text = f"用户已回答提问 {row['id']}（{row['title']}）。"
        else:
            text = f"用户已忽略提问 {row['id']}（{row['title']}）。"
    return EventInput(
        device_id=_origin(row["user_id"]).device_id,
        conversation_id=row["conversation_id"],
        layer="notification",
        type="question.answered" if status == "answered" else "question.dismissed",
        source_id="ask_user",
        producer_event_id=f"{row['id']}:{status}",
        correlation_id=row["id"],
        reply_device_id=row["answered_device_id"] or row["reply_device_id"],
        occurred_at=now,
        payload={
            "question_id": row["id"],
            "title": row["title"],
            "answered_device_id": row["answered_device_id"],
            "selected_options": selected,
            "answer_text": row["answer_text"],
            "text": text,
        },
    )


def _format_answer_text(
    row: sqlite3.Row,
    selected: list[str],
    answer_text: str,
    device_id: str,
) -> str:
    lines = [
        f"用户已回答提问 {row['id']}（{row['title']}）",
        f"原问题：{row['question']}",
        f"回答设备：{device_id}",
    ]
    if selected:
        lines.append("已选选项：" + "、".join(selected))
    if answer_text:
        lines.append("补充回答：" + answer_text if selected else "回答内容：" + answer_text)
    return "\n".join(lines)


class QuestionStore:
    def __init__(self, store: "SQLiteStore") -> None:
        self._store = store
        self._db = store._connection

    def create(
        self,
        *,
        user_id: str,
        conversation_id: str,
        run_id: str,
        call_id: str,
        title: str,
        question: str,
        options: Sequence[str] = (),
        multi_select: bool = False,
        allow_custom: bool = True,
        reply_device_id: str | None = None,
    ) -> QuestionItem:
        clean_title = title.strip()
        clean_question = question.strip()
        if not clean_title or len(clean_title) > MAX_TITLE_LEN:
            raise ValueError("提问标题不能为空且长度不超过 120 字")
        if not clean_question or len(clean_question) > MAX_QUESTION_LEN:
            raise ValueError("提问内容不能为空且长度不超过 2000 字")
        if not all(isinstance(k, str) and 0 < len(k) <= 256 for k in (run_id, call_id)):
            raise ValueError("提问需要已登记的运行和工具调用标识")
        clean_options = [opt.strip() for opt in options]
        if len(clean_options) > MAX_OPTIONS:
            raise ValueError(f"最多提供 {MAX_OPTIONS} 个预设选项")
        if any(not opt or len(opt) > MAX_OPTION_LEN for opt in clean_options):
            raise ValueError("预设选项不能为空且每项不超过 120 字")
        if len(set(clean_options)) != len(clean_options):
            raise ValueError("预设选项不能重复")
        if not clean_options and not allow_custom:
            raise ValueError("未提供预设选项时必须允许自定义输入")
        effective_multi = bool(multi_select) if clean_options else False
        options_json = json.dumps(clean_options, ensure_ascii=False, separators=(",", ":"))
        question_id = "q_" + uuid4().hex

        with self._store._transaction():
            self._store._check_scope(conversation_id, user_id)
            if (
                reply_device_id is not None
                and self._store.get_device(reply_device_id).user_id != user_id
            ):
                raise ValueError("Device belongs to a different user")
            existing = self._db.execute(
                "SELECT * FROM user_questions WHERE user_id=? AND conversation_id=? "
                "AND run_id=? AND call_id=?",
                (user_id, conversation_id, run_id, call_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["title"],
                    existing["question"],
                    existing["options"],
                    bool(existing["multi_select"]),
                    bool(existing["allow_custom"]),
                ) != (
                    clean_title,
                    clean_question,
                    options_json,
                    effective_multi,
                    bool(allow_custom),
                ):
                    raise QuestionConflict("Tool call identifier was reused with different input")
                return _record(existing)

            self._store._register_device(_origin(user_id))
            now = self._store.now_utc().timestamp()
            row = self._db.execute(
                "INSERT INTO user_questions(id,user_id,conversation_id,run_id,call_id,"
                "reply_device_id,title,question,options,multi_select,allow_custom,status,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?) RETURNING *",
                (
                    question_id,
                    user_id,
                    conversation_id,
                    run_id,
                    call_id,
                    reply_device_id,
                    clean_title,
                    clean_question,
                    options_json,
                    int(effective_multi),
                    int(bool(allow_custom)),
                    now,
                ),
            ).fetchone()
            return _record(row)

    def get(self, *, user_id: str, conversation_id: str, id: str) -> QuestionItem:
        row = self._db.execute(
            "SELECT * FROM user_questions WHERE user_id=? AND conversation_id=? AND id=?",
            (user_id, conversation_id, id),
        ).fetchone()
        if row is None:
            raise LookupError("Unknown question")
        return _record(row)

    def list(
        self,
        *,
        user_id: str,
        conversation_id: str,
        status: Literal["pending", "all"] = "pending",
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[QuestionItem], str | None]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Page size must be between 1 and 100")
        if status not in ("pending", "all"):
            raise ValueError("Invalid question status filter")
        where = "user_id=? AND conversation_id=?"
        params: list[str | int] = [user_id, conversation_id]
        if status == "pending":
            where += " AND status='pending'"
        if cursor is not None:
            if (
                not cursor.isascii()
                or not cursor.isdigit()
                or len(cursor) > 19
                or not 0 < int(cursor) < 2**63
            ):
                raise ValueError("Invalid question cursor")
            where += " AND seq<?"
            params.append(int(cursor))
        rows = self._db.execute(
            f"SELECT * FROM user_questions WHERE {where} ORDER BY seq DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        next_cursor = str(rows[limit - 1]["seq"]) if len(rows) > limit else None
        return [_record(row) for row in rows[:limit]], next_cursor

    def answer(
        self,
        *,
        user_id: str,
        conversation_id: str,
        device_id: str,
        id: str,
        selected_options: Sequence[str] = (),
        answer_text: str = "",
    ) -> QuestionItem:
        clean_selected = [opt.strip() for opt in selected_options if isinstance(opt, str)]
        clean_text = answer_text.strip()
        if len(clean_text) > MAX_ANSWER_LEN:
            raise ValueError("回答内容不能超过 4000 字")
        if len(set(clean_selected)) != len(clean_selected):
            raise ValueError("不能重复选择同一选项")
        if not clean_selected and not clean_text:
            raise ValueError("请至少选择一个选项或填写回答内容")

        with self._store._transaction():
            if self._store.get_device(device_id).user_id != user_id:
                raise ValueError("Device belongs to a different user")
            row = self._db.execute(
                "SELECT * FROM user_questions WHERE user_id=? AND conversation_id=? AND id=?",
                (user_id, conversation_id, id),
            ).fetchone()
            if row is None:
                raise LookupError("Unknown question")
            allowed_options: list[str] = json.loads(row["options"])
            if clean_selected:
                if not allowed_options or any(opt not in allowed_options for opt in clean_selected):
                    raise ValueError("包含无效的选项")
                if not bool(row["multi_select"]) and len(clean_selected) > 1:
                    raise ValueError("该提问仅允许单选")
            if clean_text and allowed_options and not bool(row["allow_custom"]):
                raise ValueError("该提问不支持自定义文本回答")

            selected_json = json.dumps(clean_selected, ensure_ascii=False, separators=(",", ":"))
            if row["status"] == "answered":
                if (
                    row["selected_options"] == selected_json
                    and row["answer_text"] == clean_text
                ):
                    return _record(row)
                raise QuestionConflict("该提问已被其他设备回答")
            if row["status"] != "pending":
                raise QuestionConflict("该提问已被忽略")

            now = self._store.now_utc()
            self._store._register_device(_origin(user_id))
            updated = self._db.execute(
                "UPDATE user_questions SET status='answered', answered_at=?, "
                "answered_device_id=?, selected_options=?, answer_text=? "
                "WHERE seq=? RETURNING *",
                (now.timestamp(), device_id, selected_json, clean_text, row["seq"]),
            ).fetchone()
            text = _format_answer_text(updated, clean_selected, clean_text, device_id)
            event = _notice(updated, now, "answered", text)
            try:
                self._store._check_event_size(event)
            except EventError:
                event = _notice(updated, now, "answered")
                self._store._check_event_size(event)
            self._store._append_event(event)
            return _record(updated)

    def dismiss(
        self,
        *,
        user_id: str,
        conversation_id: str,
        device_id: str,
        id: str,
    ) -> QuestionItem:
        with self._store._transaction():
            if self._store.get_device(device_id).user_id != user_id:
                raise ValueError("Device belongs to a different user")
            row = self._db.execute(
                "SELECT * FROM user_questions WHERE user_id=? AND conversation_id=? AND id=?",
                (user_id, conversation_id, id),
            ).fetchone()
            if row is None:
                raise LookupError("Unknown question")
            if row["status"] == "dismissed":
                return _record(row)
            if row["status"] != "pending":
                raise QuestionConflict("该提问已被回答")

            now = self._store.now_utc()
            self._store._register_device(_origin(user_id))
            updated = self._db.execute(
                "UPDATE user_questions SET status='dismissed', answered_at=?, "
                "answered_device_id=? WHERE seq=? RETURNING *",
                (now.timestamp(), device_id, row["seq"]),
            ).fetchone()
            text = (
                f"用户已忽略提问 {updated['id']}（{updated['title']}）\n"
                f"原问题：{updated['question']}\n"
                f"操作设备：{device_id}"
            )
            event = _notice(updated, now, "dismissed", text)
            try:
                self._store._check_event_size(event)
            except EventError:
                event = _notice(updated, now, "dismissed")
                self._store._check_event_size(event)
            self._store._append_event(event)
            return _record(updated)
