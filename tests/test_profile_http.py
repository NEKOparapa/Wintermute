import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from aiohttp import ClientSession

from wintermute.agent import AssistantMessage
from wintermute.config import AppConfig, ConfigError
from wintermute.config.factory import ModelSet, create_scheduler
from wintermute.config.models import EventSettings, ProfileDreamingSettings
from wintermute.events import EventGateway, SQLiteStore
from wintermute.events.runtime.profile import read_profile_prompt
from wintermute.events.runtime.profile_summary import ProfileEvidence, summarize_profile
from wintermute.interfaces.service import ConversationService

TEMPLATE = Path(__file__).resolve().parents[1] / "config" / "PROFILE.example.md"


def test_two_users_cannot_share_file_even_when_dreaming_is_disabled(tmp_path):
    settings = EventSettings(
        profile_files={"user-a": "profile.md", "user-b": "sub/../profile.md"},
        profile_dreaming=ProfileDreamingSettings(enabled=False),
    )
    with SQLiteStore(":memory:") as store, pytest.raises(ConfigError, match="separate file"):
        create_scheduler(settings, EventGateway(store), lambda _: None, base_dir=tmp_path)


@pytest.fixture
async def profile_api(tmp_path):
    terminals = [("desktop", "owner"), ("mobile", "owner"), ("other", "other"), ("guest", "guest")]
    config = AppConfig.model_validate(
        {
            "events": {
                "profile_files": {"owner": "profile.md", "other": "other/profile.md"},
                "profile_dreaming": {"enabled": False},
            },
            "interfaces": {
                "http": {
                    "enabled": True,
                    "port": 0,
                    "terminals": [
                        {"terminal_id": t, "user_id": u, "conversation_id": t, "token": "test-" + t}
                        for t, u in terminals
                    ],
                }
            },
        }
    )
    with SQLiteStore(":memory:", clock=lambda: datetime(2026, 11, 16, 16, 5, tzinfo=UTC)) as store:
        service = ConversationService(config, ModelSet(None, {}), store, base_dir=tmp_path)
        async with service, ClientSession() as client:
            host, port = service.http.addresses[0]

            async def get(terminal, *, query="", token=None):
                headers = {
                    "X-Terminal-ID": terminal,
                    "Authorization": "Bearer " + (token or "test-" + terminal),
                }
                async with client.get(
                    f"http://{host}:{port}/v1/profile{query}", headers=headers
                ) as response:
                    if response.status == 200:
                        assert "no-store" in response.headers["Cache-Control"]
                    return response.status, await response.json()

            yield get


async def test_profile_follows_authenticated_user_and_published_file(profile_api, tmp_path):
    path = tmp_path / "profile.md"
    get = profile_api
    assert (await get("desktop", token="test-other"))[0] == 401
    assert (await get("other", query="?user_id=owner"))[0] == 400
    assert (await get("other", query="?path=profile.md"))[0] == 400
    assert await get("guest") == (
        200,
        {"status": "unconfigured", "markdown": "", "effective": False},
    )
    # Preserve the sample's visible headings and fields, excluding its title and authoring notes.
    sample = TEMPLATE.read_text(encoding="utf-8").splitlines()[1:]
    initial = "\n".join(line for line in sample if not line.startswith("<!--")).strip()
    empty = (
        200,
        {"status": "empty", "markdown": re.sub(r"\n{3,}", "\n\n", initial), "effective": False},
    )
    assert await get("desktop") == empty
    assert not path.exists()
    path.write_bytes(TEMPLATE.read_bytes())
    assert await get("desktop") == empty

    published = TEMPLATE.read_text(encoding="utf-8").replace("统计时区：", "统计时区：UTC")
    path.write_text(published, encoding="utf-8")
    activity = (
        200,
        {"status": "ready", "markdown": "## 常用时间段\n- 统计时区：UTC", "effective": False},
    )
    assert await get("desktop") == await get("mobile") == activity
    assert await get("other") == empty
    assert not (tmp_path / "other").exists()

    # Shanghai local date is already the 17th; the auto entry expired after the 16th.
    note = "我今天在上海。"
    source = "evt_internal"

    class Model:
        async def stream(self, context):
            change = dict(
                op="add",
                section="近期状态",
                source=source,
                quote=note,
                text=note,
                until="2026-11-16",
            )
            yield AssistantMessage(json.dumps({"changes": [change]}, ensure_ascii=False))

    published = await summarize_profile(
        Model(),
        published,
        [ProfileEvidence(source, datetime(2026, 11, 16, tzinfo=UTC), note)],
        today=date(2026, 11, 16),
    )
    assert source in published
    path.write_text(published, encoding="utf-8")
    assert await get("desktop") == activity
    assert path.read_text(encoding="utf-8") == published

    path.write_text("## 长期偏好\n- 请发到手机。 <!-- 私有说明 -->", encoding="utf-8")
    assert await get("mobile") == (
        200,
        {"status": "ready", "markdown": "## 长期偏好\n- 请发到手机。", "effective": True},
    )
    assert read_profile_prompt(path)
    path.write_text("笔记" * 1500, encoding="utf-8")
    assert await get("desktop") == (
        200,
        {
            "status": "ready",
            "markdown": path.read_text(encoding="utf-8"),
            "effective": False,
        },
    )
    assert not read_profile_prompt(path)
    path.unlink()
    assert await get("desktop") == empty


async def test_profile_invalid_or_oversized_file_is_unavailable(profile_api, tmp_path):
    path = tmp_path / "profile.md"
    for content in (b"sensitive\xff", b"x" * 65_537):
        path.write_bytes(content)
        assert await profile_api("desktop") == (503, {"error": "profile_unavailable"})
        assert path.read_bytes() == content
