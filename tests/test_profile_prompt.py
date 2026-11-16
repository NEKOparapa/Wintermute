from pathlib import Path

from wintermute.events.runtime.profile import read_profile_prompt


def test_empty_template_and_missing_file_contribute_nothing(tmp_path):
    template = Path(__file__).resolve().parents[1] / "config" / "PROFILE.example.md"
    assert read_profile_prompt(template) == ""
    assert read_profile_prompt(tmp_path / "missing.md") == ""


def test_template_exposes_only_populated_sections(tmp_path):
    path = tmp_path / "PROFILE.md"
    path.write_text(
        "# 用户档案\n## 基本信息\n- 称呼：\n- 常用语言：中文\n"
        "## 长期偏好\n<!-- 模板说明\n第二行 -->\n"
        "## 常用时间段\n- 统计时区：UTC\n### 子栏目\n不展示\n"
        "## 近期状态\n- 周四前出差。\n",
        encoding="utf-8-sig",
    )
    prompt = read_profile_prompt(path)
    assert "当前用户的明确要求优先" in prompt
    assert "> ## 基本信息\n> - 常用语言：中文" in prompt
    assert "> ## 近期状态\n> - 周四前出差。" in prompt
    for unused in ("称呼", "第二行", "长期偏好", "统计时区", "不展示"):
        assert unused not in prompt


def test_plain_notes_are_kept_and_invalid_or_oversize_documents_are_skipped(tmp_path, caplog):
    path = tmp_path / "PROFILE.md"
    path.write_text("项目周会优先发手机。", encoding="utf-8")
    assert "> 项目周会优先发手机。" in read_profile_prompt(path)
    for data in (b"private-note\xff", b"private-note" * 1000, b"x" * 70000):
        path.write_bytes(data)
        assert read_profile_prompt(path) == ""
    assert "private-note" not in caplog.text
