"""历史媒体附件的领域引用，与 HTTP 下载实现无关。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HistoryAttachment:
    path: Path
    name: str
    content_type: str
    occurred_at: str | None = None
    window_title: str | None = None
