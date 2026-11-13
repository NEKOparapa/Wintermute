"""通过离线会议场景验证真实主模型请求中的未读边界和重启后的检查点。"""

import json
from pathlib import Path
from runpy import run_path

from model_text import event_fields

from wintermute.events import SQLiteStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "meeting_interruptions.py"


async def test_meeting_compaction_protects_unread_inputs_and_resumes(tmp_path: Path) -> None:
    meeting = run_path(str(EXAMPLE))
    capture = tmp_path / "requests.json"
    result = json.loads(await meeting["run_compaction_meeting"](tmp_path, dump_requests=capture))
    requests = json.loads(capture.read_text(encoding="utf-8"))["requests"]
    assert result["已压缩原始消息数"] == 4
    assert result["重启后新增摘要调用"] == 0
    assert len(requests) == 4
    compacted = requests[2]["messages"]
    assert result["摘要"] in compacted[0]["text"]
    assert all(m["role"] == "user" for m in compacted)
    inputs = [event_fields(m["text"]) for m in compacted[1:]]
    assert [m["来源"] for m in inputs] == [
        "活动时间线",
        "用户原话 · telegram",
        "用户原话 · telegram",
        "后台通知 · worker · reader",
    ]
    assert [m["正文"] for m in inputs[1:]] == [
        meeting["QUESTION"],
        meeting["GUIDANCE"],
        meeting["FINDING"],
    ]
    assert requests[3]["messages"][: len(compacted)] == compacted
    with SQLiteStore(tmp_path / "meeting.sqlite3") as store:
        raws = store.history("work", "owner")
        assert store.load_model_read_count("work", "owner") == len(raws) - 1
        assert store.load_context_checkpoint("work", "owner").summarized_messages == 4
