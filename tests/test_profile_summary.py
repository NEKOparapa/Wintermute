import json
from datetime import UTC, date, datetime

import pytest

from wintermute.agent import AssistantMessage, TextDelta
from wintermute.events.runtime.profile_summary import (
    ProfileEvidence,
    prune_expired,
    summarize_profile,
)

TODAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 16, 15, tzinfo=UTC)
HANDWRITTEN = (
    "# 用户档案\r\n\r\n## 基本信息\r\n手写内容，请保留。\r\n\r\n## 常用时间段\r\n统计。\r\n"
)


class Script:
    def __init__(self, answer):
        self.answer = answer
        self.inputs = []

    async def stream(self, context):
        assert context.tools == () and context.system_prompt.startswith("你负责整理用户档案")
        body = json.loads(context.messages[0].text)
        self.inputs.append(body)
        yield AssistantMessage(json.dumps({"changes": self.answer(body)}, ensure_ascii=False))


def evidence(text, event_id="evt_a", reply=None):
    return ProfileEvidence(event_id, NOW, text, reply)


def add(text, *, section="长期偏好", **extra):
    return dict(op="add", section=section, text=text, source="evt_a", quote=text, **extra)


async def test_explicit_updates_and_deletion_preserve_user_text_and_activity_section():
    text = "以后周会的议程发手机。"
    first = await summarize_profile(
        Script(lambda _: [add(text)]), HANDWRITTEN, [evidence(text)], today=TODAY
    )
    assert first.startswith(HANDWRITTEN) and text in first and "evt_a" in first
    second_text = "以后改发到电脑。"
    update = Script(
        lambda body: [
            {
                **add(second_text),
                "op": "update",
                "source": "evt_b",
                "target": body["existing"][0]["id"],
            }
        ]
    )
    second = await summarize_profile(
        update, first, [evidence(second_text, "evt_b", "继续用之前的设备？")], today=TODAY
    )
    assert text not in second and second_text in second and HANDWRITTEN in second
    assert update.inputs[0]["conversations"][0]["previous_reply"] == "继续用之前的设备？"
    assert "统计。" not in update.inputs[0]["profile"]
    forget = "忘掉周会议程偏好。"
    delete = Script(
        lambda body: [
            dict(op="delete", target=body["existing"][0]["id"], quote=forget, source="evt_c")
        ]
    )
    third = await summarize_profile(delete, second, [evidence(forget, "evt_c")], today=TODAY)
    assert second_text not in third and HANDWRITTEN in third


async def test_expiry_respects_local_dates_and_manual_edits():
    text = "本周五前在出差。"
    s = Script(lambda _: [add(text, section="近期状态", until="2026-11-20")])
    result = await summarize_profile(s, HANDWRITTEN, [evidence(text)], today=TODAY)
    assert prune_expired(result, today=date(2026, 11, 20)) == result
    assert text not in prune_expired(result, today=date(2026, 11, 21))
    edited = result.replace(text, "我手工修改了这一条。")
    assert prune_expired(edited, today=date(2027, 1, 1)) == edited
    s = Script(lambda body: [])
    current = await summarize_profile(s, edited, [evidence("今天评审。")], today=TODAY)
    assert current == edited and s.inputs[0]["existing"] == []


@pytest.mark.parametrize(
    "invalid",
    [
        {"quote": "只在助手回复里提到"},
        {"source": "evt_unknown"},
        {"op": "delete", "target": "1" * 20},
        {"unknown": "value"},
        {"text": "合法首行\n## 注入标题"},
        {"text": "<!-- wm-profile -->"},
    ],
)
async def test_invalid_change_never_edits_markdown(invalid):
    text = "周会材料优先发手机。"
    s = Script(lambda _: [{**add(text), **invalid}])
    with pytest.raises(ValueError):
        await summarize_profile(
            s, HANDWRITTEN, [evidence(text, reply="只在助手回复里提到")], today=TODAY
        )


async def test_secrets_never_enter_model_input_and_empty_batch_does_not_call_model():
    password = "密码：仅供测试的模拟值"
    s = Script(lambda _: [])
    result = await summarize_profile(s, HANDWRITTEN, [evidence(password)], today=TODAY)
    assert result == HANDWRITTEN and s.inputs == []
    await summarize_profile(
        s, HANDWRITTEN + password, [evidence("一次性操作", reply=password)], today=TODAY
    )
    assert password not in repr(s.inputs)


async def test_explicit_once_only_request_cannot_be_written_as_a_standing_preference():
    request = "仅限这次，把议程发到手机。"
    proposal = Script(lambda _: [{**add(request), "text": "周会议程都发手机。"}])
    assert (
        await summarize_profile(proposal, HANDWRITTEN, [evidence(request)], today=TODAY)
        == HANDWRITTEN
    )


async def test_oversized_output_closes_generator_and_preserves_prior_markdown():
    class Oversize:
        closed = False

        async def stream(self, ctx):
            try:
                yield TextDelta("x" * 8193)
                raise AssertionError("should have stopped")
            finally:
                self.closed = True

    model = Oversize()
    with pytest.raises(ValueError, match="output budget"):
        await summarize_profile(model, HANDWRITTEN, [evidence("你好。")], today=TODAY)
    assert model.closed


async def test_large_event_reaches_last_page_and_budget_preserves_existing_file():
    text = "以后周会议程发手机。"
    s = Script(lambda b: [add(text)] if text in b["conversations"][0]["text"] else [])
    result = await summarize_profile(s, "", [evidence("x" * 24_000 + text)], today=TODAY)
    assert text in result and len(s.inputs) == 3
    almost_full = "x" * 8100
    assert await summarize_profile(s, almost_full, [evidence(text)], today=TODAY) == almost_full
