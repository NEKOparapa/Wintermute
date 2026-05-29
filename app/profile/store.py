from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

PERSONA = "persona"
USER = "user"


class ProfileStore:
    """管理 soul/persona/user 三类长期画像文件。

    - soul：人工核心人格，只读，永不被自动流程修改。
    - persona：AI 习得人格，缺失时用模板初始化，按周自动刷新。
    - user：用户画像，缺失时用模板初始化，按日自动刷新。

    画像文件写入 ``data_dir/memories/profile/`` 下，写入前会把旧版本快照到
    ``history/`` 目录，避免一次失败的自动改写直接覆盖掉历史内容。
    """

    def __init__(
        self,
        data_dir: Path | str,
        *,
        soul_path: Path | str,
        persona_template_path: Path | str,
        user_template_path: Path | str,
    ) -> None:
        """记录画像目录与各模板路径，并准备线程锁保护读写。"""
        self.profile_dir = Path(data_dir) / "memories" / "profile"
        self.history_dir = self.profile_dir / "history"
        self.soul_path = Path(soul_path)
        self._templates = {
            PERSONA: Path(persona_template_path),
            USER: Path(user_template_path),
        }
        self._lock = threading.Lock()

    def ensure_seeded(self) -> None:
        """data 中缺失 persona/user 时，用 config 模板初始化它们。"""
        with self._lock:
            for name in (PERSONA, USER):
                self._ensure_seeded_unlocked(name)

    def read_soul(self) -> str:
        """读取人工核心人格；文件缺失时返回空串。"""
        return _read_text(self.soul_path)

    def read_persona(self) -> str:
        """读取习得人格；缺失时先用模板初始化再读取。"""
        return self._read_profile(PERSONA)

    def read_user(self) -> str:
        """读取用户画像；缺失时先用模板初始化再读取。"""
        return self._read_profile(USER)

    def write_persona(self, content: str) -> None:
        """覆盖写入习得人格，写入前快照旧版本。"""
        self._write_profile(PERSONA, content)

    def write_user(self, content: str) -> None:
        """覆盖写入用户画像，写入前快照旧版本。"""
        self._write_profile(USER, content)

    def _read_profile(self, name: str) -> str:
        with self._lock:
            self._ensure_seeded_unlocked(name)
            return _read_text(self._path_for(name))

    def _write_profile(self, name: str, content: str) -> None:
        text = content.strip()
        if not text:
            # 空内容视为无效更新，直接忽略以保护既有画像。
            return
        with self._lock:
            path = self._path_for(name)
            self._snapshot_unlocked(name, path)
            _write_text(path, text + "\n")
            logger.info("写入画像文件 name=%s path=%s", name, path)

    def _ensure_seeded_unlocked(self, name: str) -> None:
        path = self._path_for(name)
        if path.exists():
            return
        template = _read_text(self._templates[name])
        content = template if template.endswith("\n") or not template else template + "\n"
        _write_text(path, content)
        logger.info("初始化画像文件 name=%s path=%s", name, path)

    def _snapshot_unlocked(self, name: str, path: Path) -> None:
        if not path.exists():
            return
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        _write_text(self.history_dir / f"{name}-{stamp}.md", _read_text(path))

    def _path_for(self, name: str) -> Path:
        return self.profile_dir / f"{name}.md"


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    """原子写入文本：先写临时文件再 replace，避免写一半被读到。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
