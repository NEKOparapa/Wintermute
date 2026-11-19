"""以配置目录为基准解析路径，保留记忆目录的链接检查信息。"""

import os
from pathlib import Path


def _path(value: str, base_dir: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(base_dir).expanduser().resolve() / path
    return path.resolve()


def _memory_root(value: str, base_dir: str | Path) -> Path:
    """Normalize spelling without hiding links before the vault's sandbox checks."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(base_dir).expanduser().absolute() / path
    return Path(os.path.abspath(path))
