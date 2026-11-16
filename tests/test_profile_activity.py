from dataclasses import replace
from datetime import UTC, datetime, timedelta

from wintermute.events import EventGateway, SQLiteStore, Terminal
from wintermute.events.storage.profile_activity import update_activity_section


async def test_real_usage_formats_user_scope_local_midnight_and_device_overlap():
    now = datetime(2026, 11, 16, 1, 15, tzinfo=UTC)  # Monday, 09:15 local
    with SQLiteStore(":memory:") as store:
        gateway = EventGateway(store)
        for name, user in (("pc", "owner"), ("phone", "owner"), ("guest", "other")):
            gateway.register_terminal(Terminal(terminal_id=name, user_id=user))

        async def desktop(begin, finish, terminal="pc"):
            await gateway.perception.connect(terminal).observe(
                "desktop.window_span",
                {
                    "foreground_app": "browser",
                    "started_at": begin.isoformat(),
                    "ended_at": finish.isoformat(),
                    "duration_seconds": (finish - begin).total_seconds(),
                },
                occurred_at=finish,
            )

        async def mobile(begin, finish):
            seconds = int((finish - begin).total_seconds())
            text = (
                f"手机前台使用「QQ」，{begin:%Y-%m-%d %H:%M:%S} 至 "
                f"{finish:%Y-%m-%d %H:%M:%S}（UTC），约 {seconds} 秒。"
            )
            await gateway.slice.connect("phone").record(text, occurred_at=finish)

        for day in (9, 10, 11):
            begin = datetime(2026, 11, day, 1, tzinfo=UTC)
            if day != 11:
                await desktop(begin, begin + timedelta(minutes=30))
            if day != 10:
                await mobile(begin, begin + timedelta(minutes=30))
            await desktop(begin + timedelta(hours=5), begin + timedelta(hours=6), "guest")
        fri = datetime(2026, 11, 13, 15, 50, tzinfo=UTC)
        await mobile(fri, fri + timedelta(minutes=20))  # local Fri 23:50 to Sat 00:10
        stale = now - timedelta(days=35)
        await desktop(stale, stale + timedelta(minutes=30))
        await gateway.slice.connect("phone").record("之前晚上常开电脑", occurred_at=now)
        await gateway.perception.connect("phone").observe("mobile.app_activity", "前台 · 聊天")

        pattern = store.profile_activity("owner", now, "Asia/Shanghai")
        assert (pattern.weekday_days, pattern.weekend_days) == (4, 1)
        assert pattern.weekday_slots == {18} and pattern.weekend_slots is None
        assert pattern.low_activity is False
        assert "09:00–09:30" in pattern.markdown()
        assert "样本不足" in pattern.markdown()
        assert store.profile_activity(
            "owner", now + timedelta(hours=1), "Asia/Shanghai"
        ).low_activity
        assert store.profile_activity("other", now, "Asia/Shanghai").weekday_slots == {28, 29}


def test_refresh_preserves_handwritten_sections_and_handles_empty_history():
    with SQLiteStore(":memory:") as store:
        unknown = store.profile_activity("owner", datetime(2026, 11, 16, tzinfo=UTC), "UTC")
    assert unknown.low_activity is None and unknown.observed_days == 0
    template = "## 长期偏好\r\n- 原话\r\n\r\n## 常用时间段\r\n- 工作日常见活跃时段：\r\n"
    assert update_activity_section(template, unknown) == template
    pattern = replace(unknown, weekday_days=3, weekday_slots=frozenset({47}))
    note = "- 备注：工作日作息可能变化。\r\n"
    suffix = "## 近期状态\r\n- 正在出差。\r\n"
    original = template + note + suffix
    result = update_activity_section(original, pattern)
    assert result.startswith("## 长期偏好\r\n- 原话\r\n")
    assert result.endswith(note + suffix)
    assert "- 工作日常见活跃时段：23:30–24:00\r\n" in result
    assert update_activity_section(result, pattern) == result
    manual = result.replace(
        "<!-- wintermute:activity:end -->", note + "<!-- wintermute:activity:end -->"
    )
    aged = update_activity_section(manual, unknown)
    assert "23:30" not in aged and "有记录的天数：0" in aged
    assert aged.count(note) == 2 and aged.endswith(note + suffix)
