from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from wintermute.events.core.activity import REVISION_NOTE, ActivityItem, timeline_delta
from wintermute.events.core.timeline_compression import (
    TimelinePolicy,
    batch_text,
    clip,
    estimate,
    group_rules,
    merge_stats,
    raw_size,
    render_timeline,
    rule_text,
)

T0 = datetime(2026, 11, 15, 0, 58, 0, 123456, UTC)
POLICY = TimelinePolicy()


def note(n, text, at=T0, terminal="phone"):
    return ActivityItem(
        activity_id=f"test-{n}",
        user_id="user",
        conversation_id=None,
        terminal_id=terminal,
        source_id="default",
        kind="slice",
        activity_key=f"slice-{n}",
        content={"text": text},
        event_type="slice.summary",
        occurred_at=at,
        known_at=at,
        source_event_ids=(f"event-{n}",),
    )


def visit(n, start, seconds, app="QQ", title="", terminal="pc"):
    end = start + timedelta(seconds=seconds)
    return note(n, "", end, terminal).model_copy(
        update={
            "kind": "perception",
            "activity_key": "desktop",
            "event_type": "state.observed",
            "content": {
                "subject": f"device:{terminal}",
                "state_key": "desktop.window_span",
                "value": {
                    "foreground_app": app,
                    "window_title": title,
                    "duration_seconds": seconds,
                    "started_at": start.isoformat(),
                    "ended_at": end.isoformat(),
                },
            },
        }
    )


def test_six_desktop_batches_fold_common_titles_before_ai_threshold():
    batches = [
        batch_text([visit(i, T0 + timedelta(hours=i), 20, "chrome", "GitHub")], POLICY)
        for i in range(6)
    ]
    text = group_rules(batches, POLICY)
    assert text.count("GitHub") == 1
    assert "chrome 6次，约120秒" in text and estimate(text) < POLICY.ai_threshold


def test_rule_stats_separate_devices_days_gaps_and_retain_distinct_titles():
    items = [
        visit(0, T0, 30, title="GitHub"),
        visit(1, T0, 10, terminal="laptop"),
        visit(2, T0 + timedelta(minutes=5), 60, title="文档"),
        visit(3, T0 + timedelta(minutes=45), 20, title="日历"),
        visit(4, T0 + timedelta(days=1), 7, title="会议"),
    ]
    b = batch_text(items, POLICY)
    assert b.count == 5 and b.start == T0 and b.end == items[-1].occurred_at
    assert "QQ 2次，约90秒" in b.rule_text and "文档、GitHub" in b.rule_text
    assert "QQ 1次，约10秒" in b.rule_text and "laptop" in b.rule_text
    assert "QQ 1次，约20秒" in b.rule_text and "QQ 1次，约7秒" in b.rule_text
    assert b.rule_text.count("| 感知 · pc") == 3
    # Multi-level totals use the same durable facts, not incidental timing gaps.
    groups = [batch_text([i], POLICY) for i in items]
    group = replace(b, stats_text=merge_stats(groups), ai_text="会议和查文档。")
    result = render_timeline([group], POLICY)
    assert result is not None
    assert "2026-11-15 感知 · pc | QQ 3次，约110秒" in result.text
    assert "2026-11-16 感知 · pc | QQ 1次，约7秒" in result.text
    assert "会议和查文档" in result.text


def test_generic_states_and_slices_collapse_repeats_but_keep_transitions():
    def state(n, value):
        return note(n, "", at=T0 + timedelta(seconds=n)).model_copy(
            update={
                "kind": "perception",
                "event_type": "state.observed",
                "content": {"state_key": "presence", "subject": "device:phone", "value": value},
            }
        )

    items = [
        state(0, "到达"),
        state(1, "到达"),
        state(2, "离开"),
        state(3, "到达"),
        note(4, "周会"),
        note(5, "周会"),
    ]
    text = rule_text(items, POLICY)
    assert "到达（2次）\npresence：离开\npresence：到达" in text
    assert "周会（2次）" in text


def test_small_timeline_preserves_original_message_and_revision_suffix():
    first, second = note(1, "开始会议"), note(2, "已确认截止时间", T0 + timedelta(minutes=1))
    msg = render_timeline([([first], None), ([second], None)], POLICY)
    assert msg == timeline_delta([(first, False), (second, False)])
    revision = render_timeline([([first, second], None)], POLICY, revision=True)
    assert revision is not None and revision.text == REVISION_NOTE + "\n" + msg.text
    assert msg.model_text().endswith("来源：活动时间线\n时间：2026-11-15T00:59:00.123456Z")
    assert raw_size(first) == estimate(msg.text.splitlines()[0])
    assert render_timeline([([], None)], POLICY) is None


@pytest.mark.parametrize("terminal", ["http-desktop", "office-laptop"])
def test_window_span_uses_its_source_device_sampled_duration_and_only_one_interval(terminal):
    start, end = "2026-11-15T04:54:50.137Z", "2026-11-15T04:55:20.011Z"
    reported = datetime.fromisoformat(end)
    item = visit(
        1, datetime.fromisoformat(start), 23, "LeagueClientUx", "League of Legends", terminal
    ).model_copy(update={"occurred_at": reported, "known_at": reported})
    item.content["value"]["ended_at"] = end
    item.content["value"]["started_at"] = start
    body = f"{start} 至 {end}，约 23 秒。应用「LeagueClientUx」，窗口标题「League of Legends」。"
    expected = f"{body} | 来源设备：{terminal}"
    message = render_timeline([([item], None)], POLICY)
    assert message and message.text == expected
    assert (
        message.model_text()
        == expected + "\n\n---\n来源：活动时间线\n时间：2026-11-15T04:55:20.011000Z"
    )
    assert raw_size(item) == estimate(expected)

    later = reported + timedelta(minutes=1)
    delayed = item.model_copy(update={"occurred_at": later, "known_at": later})
    expiring = item.model_copy(update={"valid_until": later})
    skewed = item.model_copy(update={"known_at": datetime.fromisoformat(start)})
    for unusual, annotation in [(delayed, "04:56:20"), (expiring, "有效至"), (skewed, "待确认")]:
        message = render_timeline([([unusual], None)], POLICY)
        assert message and annotation in message.text and body in message.text
        assert message.text.count(terminal) == 1


@pytest.mark.parametrize("budget", [128, 180, 257, 400, 800])
@pytest.mark.parametrize("revise", [False, True])
def test_bounded_unicode_tail_and_real_message_envelope(budget, revise):
    policy = replace(POLICY, delivery_tokens=budget)
    older = [
        batch_text(
            [note(i, "需求评审" + "旧资料" * 200, T0 + timedelta(days=i), "office-pc")], policy
        )
        for i in range(8)
    ]
    newest = note(
        20, "最终决定：周五发版。\n" + "会议原文" * 900, T0 + timedelta(days=9), "meeting-phone"
    )
    all_data = older + [batch_text([newest], policy)]
    for segments in (all_data, [([newest], None)]):
        msg = render_timeline(segments, policy, revision=revise)
        assert msg is not None and estimate(msg.model_text()) <= budget
        assert "最终" in msg.text and "meeting-phone" in msg.text
    if budget >= 400:
        msg = render_timeline(all_data, policy, revision=revise)
        assert msg is not None and "office-pc | 需求评审" in msg.text and "较早" in msg.text


def test_estimated_budget_is_enforced_without_breaking_unicode():
    for budget in range(12):
        text = clip("a会🕒会议", budget)
        assert estimate(text) <= budget and "\ufffd" not in text
    with pytest.raises(ValueError, match="delivery_tokens"):
        TimelinePolicy(delivery_tokens=0)
    with pytest.raises(ValueError, match="timezone"):
        TimelinePolicy(timezone="not-a-zone")
