"""Bounded, credential-safe Markdown audit entries."""

import difflib
from datetime import datetime

from purin.memory.security import redact


def _inline(value: str) -> str:
    """Metadata can contain Unicode separators even when literal newlines were rejected."""
    return " ".join(redact(value).splitlines()).replace("`", "'")


def audit_entry(
    *,
    now: datetime,
    actor: str,
    channel: str,
    action: str,
    path: str,
    reason: str,
    before: str,
    after: str,
    detail: str = "",
) -> str:
    identity = _inline(actor) + (f" ({_inline(channel)})" if actor == "memory_agent" else "")
    reason = _inline(reason)
    heading = f"## {now:%H:%M:%S} | {identity} | {_inline(action)} | `{_inline(path)}`\n"
    summary = (
        f"{len(before.splitlines())} → {len(after.splitlines())} 行，"
        f"{len(before.encode('utf-8'))} → {len(after.encode('utf-8'))} bytes"
    )
    parts = [heading, f"- **原因**：{reason}\n", f"- **摘要**：{summary}\n"]
    if detail:
        parts.append(f"- **详情**：{_inline(detail)}\n")
    changes = [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    if changes:
        parts.append("- **变更**：\n")
        for line in changes[:12]:
            safe = _inline(line)[:240]
            parts.append(f"  - `{safe}`\n")
        if len(changes) > 12:
            parts.append("  - [其余差异已省略]\n")
    return "".join(parts) + "\n"
