"""Historical import acceptance, isolation, persistence and preprocessing regression tests."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr, ValidationError

from wintermute.config.models import HTTPSettings, HTTPTerminalSettings
from wintermute.events import EventGateway, EventInput, SQLiteStore, Terminal
from wintermute.events.core.types import EventError
from wintermute.events.records.preprocess import aggregate, preprocess
from wintermute.events.storage.records import RecordBatchInput, RecordConflict, RecordInput
from wintermute.interfaces.http import HTTPInterface
from wintermute.memory import MemoryVault

NOW = datetime(2026, 11, 19, 8, tzinfo=UTC)


def _batch(content="project notes", *, external_id="page-1", **kwargs):
    return RecordBatchInput(
        record_type="document",
        items=[RecordInput(external_id=external_id, occurred_at=NOW, content=content)],
        **kwargs,
    )


@pytest.fixture
def store(tmp_path):
    value = SQLiteStore(
        tmp_path / "events.sqlite", clock=lambda: NOW, records_storage_path=tmp_path / "records"
    )
    value.register_terminal(Terminal(terminal_id="phone", user_id="alice"))
    value.register_terminal(Terminal(terminal_id="other", user_id="bob"))
    yield value
    value.close()


def test_batch_idempotence_and_source_external_version_scope(store):
    first = store.records.submit("phone", _batch(batch_id="sync-1"))
    assert first.accepted_items == 1
    assert store.records.submit("phone", _batch(batch_id="sync-1")) == first
    duplicate = store.records.submit("phone", _batch())
    assert (duplicate.accepted_items, duplicate.deduplicated_items) == (0, 1)
    assert duplicate.status == "completed"
    assert store.records.submit("phone", _batch(source_name="work")).accepted_items == 1
    assert store.records.submit("phone", _batch(external_id="page-2")).accepted_items == 1
    assert store.records.submit("phone", _batch("new content")).accepted_items == 1
    assert store.records.submit("other", _batch()).accepted_items == 1
    with pytest.raises(RecordConflict):
        store.records.submit("phone", _batch("changed", batch_id="sync-1"))
    with pytest.raises(RecordConflict):
        store.records.submit("other", _batch(batch_id="sync-1"))
    with pytest.raises(LookupError):
        store.records.get("bob", first.batch_id)


def test_record_import_does_not_create_live_events_or_jobs(store):
    gateway = EventGateway(store)
    calls = []
    gateway.subscribe(lambda: calls.append(True))
    asyncio.run(gateway.record.connect("phone").submit(_batch()))
    assert calls == [True]
    assert not store.list_jobs()
    assert store._connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    for table in ("activity_items", "merged_entries", "merged_batches"):
        assert store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    with pytest.raises(EventError, match="dedicated"):
        store.append(
            EventInput(
                terminal_id="phone",
                producer_event_id="generic",
                layer="record",
                type="record.batch",
                payload={},
            )
        )


def test_large_payload_spooling_integrity_and_opaque_path_identifiers(store, tmp_path):
    body = "中文历史文档" * 60000
    batch = store.records.submit("phone", _batch(body, batch_id="../../escaped"))
    item = store.records.pending("alice")[0]
    assert store.records.payload(item) == body
    row = store._connection.execute("SELECT payload_json,payload_path FROM record_items").fetchone()
    assert row["payload_json"] is None
    path = (tmp_path / "records" / row["payload_path"]).resolve()
    assert path.is_relative_to(tmp_path / "records")
    assert batch.batch_id == "../../escaped"
    path.write_text('"modified"', encoding="utf-8")
    with pytest.raises(EventError, match="integrity"):
        store.records.payload(item)


def test_symlink_storage_is_rejected(store, tmp_path):
    root = tmp_path / "records"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    try:
        (root / "alice").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symbolic links requires Windows developer mode or privileges")
    with pytest.raises(EventError, match="symbolic"):
        store.records.submit("phone", _batch("x" * 9000))
    assert not list(outside.iterdir())
    assert store.records.list("alice")[0] == []


def test_processing_counts_three_attempts_and_isolation(store):
    batch = store.records.submit(
        "phone",
        RecordBatchInput(
            record_type="sms",
            items=[
                RecordInput(occurred_at=NOW, content="old", external_id="1"),
                RecordInput(occurred_at=NOW - timedelta(days=1), content="older", external_id="2"),
                RecordInput(occurred_at=NOW + timedelta(days=1), content="new", external_id="3"),
            ],
        ),
    )
    first, second, third = store.records.pending("alice")
    assert first.external_id == "2"
    with pytest.raises(LookupError):
        store.records.mark("bob", [first.item_id], status="processed")
    store.records.mark("alice", [first.item_id], status="filtered", error="verification_code")
    store.records.mark("alice", [second.item_id], status="processed")
    for attempt in range(3):
        store.records.fail("alice", [third.item_id], error="secret should never appear")
        if attempt < 2:
            assert store.records.pending("alice")[0].attempts == attempt + 1
    state = store.records.get("alice", batch.batch_id)
    assert (state.filtered_items, state.processed_items, state.failed_items) == (1, 1, 1)
    assert state.status == "partial_failed"
    assert state.error == "record_processing_failed"
    assert store.records.pending("alice") == ()
    store.records.mark("alice", [first.item_id], status="processed")
    assert store.records.get("alice", batch.batch_id).filtered_items == 1


def test_payloads_survive_reopen_and_import_pagination(tmp_path):
    path = tmp_path / "db.sqlite"
    store = SQLiteStore(path, clock=lambda: NOW)
    store.register_terminal(Terminal(terminal_id="p", user_id="u"))
    for index in range(3):
        store.records.submit("p", _batch("x" * 9000, external_id=str(index)))
    store.close()
    reopened = SQLiteStore(path, clock=lambda: NOW)
    try:
        assert len(reopened.records.pending("u")) == 3
        assert reopened.records.payload(reopened.records.pending("u")[0]) == "x" * 9000
        page, cursor = reopened.records.list("u", limit=2)
        tail, next_cursor = reopened.records.list("u", limit=2, cursor=cursor)
        assert len(page) == 2 and len(tail) == 1 and next_cursor is None
        assert set(b.batch_id for b in page).isdisjoint(b.batch_id for b in tail)
    finally:
        reopened.close()


def test_contacts_diff_uses_only_processed_not_newer_history(store):
    def contact(when, title):
        return RecordBatchInput(
            record_type="contacts",
            items=[
                RecordInput(
                    external_id="person", occurred_at=when, content={"name": "张三", "job": title}
                )
            ],
        )

    store.records.submit("phone", contact(NOW, "工程师"))
    first = store.records.pending("alice")[0]
    store.records.mark("alice", [first.item_id], status="processed")
    store.records.submit("phone", contact(NOW + timedelta(days=1), "经理"))
    second = store.records.pending("alice")[0]
    previous = store.records.previous_payload(second)
    result = preprocess(second, store.records.payload(second), previous=previous)
    assert '"job": "经理"' in result.texts[0]
    assert '"name"' not in result.texts[0]
    store.records.submit("phone", contact(NOW - timedelta(days=1), "学生"))
    oldest = store.records.pending("alice")[0]
    assert store.records.previous_payload(oldest) is None


@pytest.mark.parametrize(
    ("kind", "content", "reason"),
    [
        ("sms", "您的验证码是123456，切勿告知他人", "verification_code"),
        ("sms", "大促优惠，请回复T退订", "marketing"),
        ("sms", "您的取件码是12345", "delivery_notification"),
        ("call_log", {"direction": "missed", "duration": 0, "is_spam": True}, "missed_spam_call"),
        ("contacts", {"phone": "12345"}, "unnamed_contact"),
        ("notion", "# 请输入内容", "template_placeholder"),
        ("recording_list", {"duration": 2}, "empty_short_recording"),
        ("document", "   ", "empty_document"),
    ],
)
def test_preprocessing_filters_noise(store, kind, content, reason):
    store.records.submit(
        "phone",
        RecordBatchInput(record_type=kind, items=[RecordInput(occurred_at=NOW, content=content)]),
    )
    item = store.records.pending("alice")[0]
    result = preprocess(item, store.records.payload(item))
    assert result.filtered_reason == reason
    assert not result.texts


def test_long_document_chunking_preserves_text_and_bounds(store):
    content = "## First\n" + "长文" * 10000 + "\n## Second\n" + "尾部" * 2000
    store.records.submit("phone", _batch(content))
    item = store.records.pending("alice")[0]
    chunks = preprocess(item, content, max_chars=2000).texts
    assert len(chunks) > 10 and all(len(chunk) <= 2000 for chunk in chunks)
    bodies = [chunk.split("\n", 3)[-1] for chunk in chunks]
    assert "".join(bodies) == content


def test_call_aggregation_preserves_sources_timestamps_and_acknowledgements(store):
    batch = store.records.submit(
        "phone",
        RecordBatchInput(
            record_type="call_log",
            items=[
                RecordInput(
                    external_id=str(index),
                    occurred_at=NOW + timedelta(hours=index),
                    content={"contact": "张三", "direction": "outgoing", "duration": seconds},
                )
                for index, seconds in enumerate((30, 60, 120))
            ],
        ),
    )
    items = store.records.pending("alice")
    evidence = [
        (
            item,
            item.model_dump_json() + "\n" + preprocess(item, store.records.payload(item)).texts[0],
        )
        for item in items
    ]
    text = aggregate(evidence, payload=store.records.payload)
    assert len(text) <= len("\n\n".join(value for _, value in evidence))
    group = json.loads(text.split("\n", 1)[1])[0]
    assert group["count_in_this_batch"] == 3
    assert group["total_duration_seconds"] == 210
    assert group["last_contact_at"] == (NOW + timedelta(hours=2)).isoformat()
    assert [row["item_id"] for row in group["records"]] == [item.item_id for item in items]
    assert [row["occurred_at"] for row in group["records"]] == [
        item.occurred_at.isoformat() for item in items
    ]
    ids = [item.item_id for item in items]
    store.records.start("alice", ids)
    assert store.records.get("alice", batch.batch_id).status == "processing"
    store.records.fail("alice", ids, error="model_unavailable")
    assert len(store.records.pending("alice")) == 3
    store.records.mark("alice", ids, status="processed")
    assert store.records.get("alice", batch.batch_id).processed_items == 3


def test_sms_aggregation_respects_contact_day_and_does_not_expand_fragments(store):
    store.records.submit(
        "phone",
        RecordBatchInput(
            record_type="sms",
            items=[
                RecordInput(
                    external_id=str(index),
                    occurred_at=NOW + timedelta(hours=index),
                    content={"sender": "张三", "body": f"项目约定{index}"},
                )
                for index in (0, 1, 24)
            ],
        ),
    )
    items = store.records.pending("alice")
    evidence = [
        (
            item,
            item.model_dump_json() + "\n" + preprocess(item, store.records.payload(item)).texts[0],
        )
        for item in items
    ]
    text = aggregate(evidence, payload=store.records.payload)
    groups = json.loads(text.split("\n", 1)[1])
    assert [len(group["records"]) for group in groups] == [2, 1]
    assert groups[0]["source_name"] == "default"
    assert groups[0]["records"][1]["text"] == "项目约定1"
    partial = [(items[0], "分片 1/2\n部分短信")]
    assert aggregate(partial, payload=store.records.payload) == partial[0][1]


def test_schema_rejects_naive_time_and_extras():
    with pytest.raises(ValidationError):
        RecordInput.model_validate({"occurred_at": "2026-11-19T08:00:00", "content": "x"})
    with pytest.raises(ValidationError):
        RecordBatchInput.model_validate({"record_type": "document", "items": [], "user_id": "x"})


def test_http_large_batch_auth_isolation_and_progress(tmp_path):
    async def scenario():
        store = SQLiteStore(tmp_path / "db.sqlite", records_storage_path=tmp_path / "records")
        settings = HTTPSettings(
            enabled=True,
            terminals=[
                HTTPTerminalSettings(
                    terminal_id="phone",
                    user_id="alice",
                    conversation_id="ca",
                    token=SecretStr("alice-token"),
                ),
                HTTPTerminalSettings(
                    terminal_id="other",
                    user_id="bob",
                    conversation_id="cb",
                    token=SecretStr("bob-token"),
                ),
            ],
        )
        interface = HTTPInterface(
            settings,
            EventGateway(store),
            base_dir=tmp_path,
            memory_vaults={"alice": MemoryVault(tmp_path / "memory")},
        )
        client = TestClient(TestServer(interface.create_app()))
        await client.start_server()
        headers = {"X-Terminal-ID": "phone", "Authorization": "Bearer alice-token"}
        other = {"X-Terminal-ID": "other", "Authorization": "Bearer bob-token"}
        try:
            payload = _batch("历史文档" * 30000, batch_id="large-http").model_dump(mode="json")
            assert len(json.dumps(payload).encode()) > 256 * 1024
            denied = await client.post("/v1/records/batches", json=payload)
            assert denied.status == 401
            accepted = await client.post("/v1/records/batches", headers=headers, json=payload)
            assert accepted.status == 202, await accepted.text()
            result = await accepted.json()
            assert result["accepted_items"] == 1
            replay = await client.post("/v1/records/batches", headers=headers, json=payload)
            assert replay.status == 202
            assert (await replay.json())["batch_id"] == result["batch_id"]
            hidden = await client.get("/v1/records/batches/large-http", headers=other)
            assert hidden.status == 404
            blocked = await client.post("/v1/records/batches", headers=other, json=payload)
            assert blocked.status == 503
            item = store.records.pending("alice")[0]
            store.records.mark("alice", [item.item_id], status="processed")
            progress = await client.get("/v1/records/batches/large-http", headers=headers)
            assert (await progress.json())["status"] == "completed"
            listing = await client.get("/v1/records/batches", headers=headers)
            assert len((await listing.json())["items"]) == 1
            invalid = await client.get("/v1/records/batches?limit=2&limit=3", headers=headers)
            assert invalid.status == 400
            assert (await client.get("/v1/records/items")).status == 401
            timeline = await client.get("/v1/records/items", headers=headers)
            metadata = await timeline.json()
            assert metadata["items"][0]["item_id"] == item.item_id
            assert metadata["items"][0]["status"] == "processed"
            assert "user_id" not in metadata["items"][0]
            assert "payload_path" not in metadata["items"][0]
            other_page = await client.get("/v1/records/items", headers=other)
            assert (await other_page.json())["items"] == []
            route = f"/v1/records/items/{item.item_id}"
            assert (await client.get(route, headers=other)).status == 404
            rebuilt, offset = "", 0
            while offset is not None:
                response = await client.get(route, params={"offset": offset}, headers=headers)
                assert response.status == 200
                page = await response.json()
                assert page["offset"] == offset
                assert len(page["text"]) <= 16000
                rebuilt += page["text"]
                offset = page["next_offset"]
            assert rebuilt == payload["items"][0]["content"]
            for query in ("offset=-1", "offset=9999999999", "limit=64001", "offset=0&offset=1"):
                assert (await client.get(route + "?" + query, headers=headers)).status == 400
            for query in ("record_type=nope", "limit=0", "cursor=bad", "limit=2&limit=3"):
                invalid = await client.get("/v1/records/items?" + query, headers=headers)
                assert invalid.status == 400
            assert not store.list_jobs()
        finally:
            await client.close()
            await interface.aclose()
            store.close()

    asyncio.run(scenario())


def test_record_timeline_keyset_time_ties_filters_and_user_isolation(store):
    for terminal in ("phone", "other"):
        for kind in ("sms", "document"):
            store.records.submit(
                terminal,
                RecordBatchInput(
                    record_type=kind,
                    items=[
                        RecordInput(occurred_at=NOW + timedelta(hours=hour), content=str(index))
                        for index, hour in enumerate((2, 0, 1, 1))
                    ],
                ),
            )
    expected = sorted(
        store.records.pending("alice"),
        key=lambda item: (item.occurred_at, item.item_id),
        reverse=True,
    )
    first, cursor = store.records.list_items("alice", limit=3)
    assert first == expected[:3] and cursor == first[-1].item_id
    # A new import at the head must not shift later pages or duplicate existing records.
    store.records.submit("phone", _batch("new record", external_id="fresh"))
    second, cursor = store.records.list_items("alice", limit=3, cursor=cursor)
    assert second == expected[3:6]
    remaining, cursor = store.records.list_items("alice", limit=3, cursor=cursor)
    assert len(remaining) == 3 and cursor is None
    assert {item.item_id for item in first + second}.isdisjoint(
        item.item_id for item in remaining
    )
    sms, _ = store.records.list_items("alice", record_type="sms")
    assert len(sms) == 4 and all(item.record_type == "sms" for item in sms)
    with pytest.raises(ValueError):
        store.records.list_items("alice", cursor=sms[0].item_id, record_type="document")
    with pytest.raises(ValueError):
        store.records.list_items("bob", cursor=first[0].item_id)
    with pytest.raises(LookupError):
        store.records.get_item("bob", first[0].item_id)
