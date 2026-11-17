"""A badge may change freely; a single new chat line must be read."""

import hashlib
import json
from datetime import UTC, datetime
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from wintermute.agent import AssistantMessage
from wintermute.events.core.types import EventRecord
from wintermute.events.slices.compression import SliceCompressionError
from wintermute.events.slices.image import ImageSliceProcessor, ScreenshotReference
from wintermute.events.slices.image_compare import badge_regions


class Vision:
    def __init__(self):
        self.calls = []
        self.reply = ""

    async def stream(self, context):
        self.calls.append(context)
        yield AssistantMessage(self.reply)


def frame(tmp_path, *, count=1, next_line=False, size=(512, 512)):
    im = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(im)
    draw.rectangle((64, 64, 95, 79), fill="red")
    draw.text((66, 64), str(count), fill="white")
    draw.text((224, 64), "Meeting: can you invite me?", fill="black")
    if next_line:
        draw.text((224, 88), "Sent.", fill="black")
    out = BytesIO()
    im.save(out, "JPEG", quality=90, subsampling=0)
    data = out.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    (tmp_path / (sha + ".jpg")).write_bytes(data)
    return EventRecord(
        event_id=sha,
        producer_event_id=sha,
        source_id="app_screenshot",
        terminal_id="pc",
        user_id="u",
        received_at=datetime.now(UTC),
        layer="slice",
        type="slice.image",
        payload={"sha256": sha, "capture": {"platform": "android", "app": "QQ", "title": "QQ"}},
    )


async def test_exact_or_badge_only_skips_model_but_single_line_edit_and_new_chat_are_read(tmp_path):
    vision = Vision()
    processor = ImageSliceProcessor(vision, tmp_path, timeout=1, max_chars=1200)
    first = frame(tmp_path)
    vision.reply = json.dumps(
        {
            "subject": "同事",
            "facts": ["对方询问能否邀请。"],
            "noise_regions": [[124, 124, 189, 158]],
        }
    )
    start = await processor.observe(first)
    ref = ScreenshotReference(
        first.payload["sha256"], start.subject, start.facts, start.noise_regions
    )
    assert not (await processor.observe(first, ref)).recognized
    badge = frame(tmp_path, count=2)
    skipped = await processor.observe(badge, ref)
    assert skipped.facts == () and not skipped.recognized
    assert len(vision.calls) == 1

    third = frame(tmp_path, count=2, next_line=True)
    vision.reply = json.dumps(
        {"subject": "同事", "facts": ["对方询问能否邀请。", "用户表示已发送。"]}
    )
    changed = await processor.observe(third, ref)
    assert changed.recognized and not changed.changed_subject
    assert changed.facts == ("用户表示已发送。",) and len(vision.calls) == 2
    assert "对方询问能否邀请。" in vision.calls[-1].messages[0].text

    # The same words in another conversation are an independent observation.
    vision.reply = json.dumps({"subject": "另一位同事", "facts": ["对方询问能否邀请。"]})
    moved = await processor.observe(third, ref)
    assert moved.changed_subject and moved.facts == ("对方询问能否邀请。",)


async def test_uncertain_layout_checks_full_image_and_plain_text_facts(tmp_path):
    vision = Vision()
    processor = ImageSliceProcessor(vision, tmp_path, timeout=1, max_chars=1200)
    first = frame(tmp_path)
    invalid = [[0, 0, 330, 1000], [450, 450, 485, 490], [False, 1, 2, 3]]
    assert not badge_regions(invalid)
    ref = ScreenshotReference(
        first.payload["sha256"], "同事", ("测试",), tuple(map(tuple, invalid))
    )
    vision.reply = '{"subject":"同事","facts":[]}'
    badge = frame(tmp_path, count=2)
    assert (await processor.observe(badge, ref)).recognized
    assert (await processor.observe(frame(tmp_path, size=(600, 512)), ref)).recognized
    vision.reply = "完整错误代码 E13，截止日期 4 月 6 日。"
    # Plain-text vision endpoints remain usable and invalid structured output is retriable.
    assert (await processor.observe(badge)).facts == (vision.reply,)
    vision.reply = '{"subject":"同事","facts":'
    with pytest.raises(SliceCompressionError):
        await processor.observe(badge)


async def test_reported_action_uses_new_visible_evidence_and_keeps_negations(tmp_path):
    vision = Vision()
    processor = ImageSliceProcessor(vision, tmp_path, timeout=1, max_chars=1200)
    first = frame(tmp_path)
    quote = "我还有pro账号，可以拉你进家庭组"
    vision.reply = json.dumps(
        {
            "subject": "小林",
            "facts": [
                {"text": "表示可以邀请小林加入家庭组。", "speaker": "用户", "evidence": quote},
                {"text": "我尚未发送邀请链接。", "speaker": "用户", "evidence": "链接尚未发送。"},
                {"text": "询问如何加入。", "speaker": "对方", "evidence": "怎么加入？"},
            ],
        }
    )
    initial = await processor.observe(first)
    assert initial.facts[0] == "用户在聊天中表示可以邀请小林加入家庭组。"
    assert initial.facts[2] == "对方在聊天中询问如何加入。"
    ref = ScreenshotReference(
        first.payload["sha256"],
        initial.subject,
        initial.facts,
        initial.noise_regions,
        initial.evidence_keys,
    )
    altered = frame(tmp_path, next_line=True)
    vision.reply = json.dumps(
        {
            "subject": "小林",
            "facts": [
                {
                    "text": "我已回复小林可以邀请他加入家庭组。",
                    "speaker": "我方",
                    "evidence": quote,
                },
                {"text": "我已发送邀请链接。", "speaker": "用户", "evidence": "链接已经发送。"},
            ],
        }
    )
    change = await processor.observe(altered, ref)
    assert change.facts == ("用户在聊天中表示，已发送邀请链接。",)
    assert len(change.evidence_keys) == 1 and "链接已经发送。" in change.evidence_keys[0]
    message = vision.calls[-1].messages[0].text
    assert quote in message and "像素变化外框" in message
