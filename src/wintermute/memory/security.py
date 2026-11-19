"""Memory permissions and credential guards, independent of model instructions."""

import re
from typing import Literal

type MemoryActor = Literal["main_agent", "memory_agent", "human_api"]
AREAS = ("agent-character", "about-user", "note", "log")
MAX_FILE_BYTES = 128 * 1024

_SECRET = re.compile(
    r"(?:sk-[\w-]{8,}|gh[pousr]_[\w]{10,}|github_pat_[\w]{10,}|AKIA[0-9A-Z]{16}|"
    r"Bearer\s+[a-z0-9._+/=-]{6,}|eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+)|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:密码|密钥|口令|令牌|password|passwd|secret|token|api[ _-]?key)"
    r"\s*(?:[:：=]|为|是|\bis\b)|"
    r"(?:验证码|校验码|动态码|verification\s*code|one[ -]time\s*(?:password|code)|OTP)"
    r"[^\n\d]{0,24}\d{4,8}(?!\d)|"
    r"(?<!\d)\d{4,8}[^\n\d]{0,24}(?:验证码|校验码|动态码|verification\s*code|OTP)|"
    r"(?:银行卡|信用卡|credit\s*card)\s*(?:号|number)?\s*[:：=]?\s*(?:\d[ -]?){13,19}",
    re.IGNORECASE,
)


def contains_secret(text: str) -> bool:
    return _SECRET.search(text) is not None


def redact(text: str) -> str:
    """Never copy an existing credential-bearing line into an audit log."""
    return "\n".join(
        "[敏感信息已隐藏]" if contains_secret(line) else line for line in text.splitlines()
    )


def check_write_permission(
    path: str,
    *,
    actor: MemoryActor,
    channel: str,
    action: str,
    allow_character_write: bool,
) -> None:
    if actor not in {"main_agent", "memory_agent", "human_api"}:
        raise PermissionError("Unknown memory actor")
    area = path.split("/", 1)[0]
    if area == "log" or path.casefold() == "about-user/activity.md":
        raise PermissionError("Audit logs and activity.md are maintained by the program")
    if actor == "human_api":
        return
    if area == "agent-character" and (not allow_character_write or channel != "conversation"):
        raise PermissionError("Character changes require an explicit first-party instruction")
    if action in {"delete", "rename"} and area != "note":
        raise PermissionError("Agents may only delete or rename files within note/")


def validate_content(content: str) -> None:
    if "\x00" in content:
        raise ValueError("Memory content must not contain NUL bytes")
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("Memory file exceeds the 128 KiB limit")
    if contains_secret(content):
        raise ValueError("Memory content contains a credential or verification code; redact it")
