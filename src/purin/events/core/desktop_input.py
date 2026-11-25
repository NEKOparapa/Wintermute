"""Content rules applied to desktop input before it contributes to a visit."""

import re
from typing import Literal

from pydantic import JsonValue

type InputAction = Literal["kept", "normalized", "redacted", "empty", "too_long", "invalid_time"]

_SECRET = "[已脱敏:密钥]"
_TOKEN = "[已脱敏:Token]"
_CARD = "[已脱敏:银行卡]"
_CODE = "[已脱敏:验证码]"
_MAX_LENGTH = 500
_MASKS = re.compile("|".join(map(re.escape, (_SECRET, _TOKEN, _CARD, _CODE))))
_SPACE = re.compile(r"[\s\x00-\x1f\x7f-\x9f\ud800-\udfff]+")
_FLAGS = re.ASCII | re.IGNORECASE
_STANDALONE_CODE = re.compile(r"\d{4,8}|\d{3}[\s-]\d{3}|\d(?:[\s-]\d){3,7}", re.ASCII)
_KEYS = re.compile(
    r"(?i:-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$))"
    r"|\b(?:sk-(?:proj-|ant-|live-|test-|svcacct-)?[\w-]{16,}"
    r"|(?:pk|rk)_(?:live|test)_[a-zA-Z0-9]{16,}|(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA)"
    r"[0-9A-Z]{16}|AIza[\w-]{32,}|gh[pousr]_[a-zA-Z0-9]{30,}|github_pat_\w{22,}"
    r"|glpat-[\w-]{20,}|xox[baprs]-[a-zA-Z0-9-]{10,}|(?:hf|npm)_[a-zA-Z0-9]{20,}"
    r"|LTAI[a-zA-Z0-9]{12,}|AKID[a-zA-Z0-9]{20,}|\d{8,10}:[\w-]{30,})\b",
    re.ASCII,
)
_JWT = re.compile(r"\beyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,}\b", re.ASCII)
_LABELED_TOKEN = re.compile(
    r"""(\b(?:(?:access|refresh|id|auth|session|api|bearer|csrf|device)[_-]?)?token"""
    r"""\s*(?:is\s+|[:=：]\s*))(['"]?)(?!\[已脱敏:)([^\s'",;，；。]{8,})\2""",
    _FLAGS,
)
_LABELED_KEY = re.compile(
    r"(\b(?:api[_-]?(?:key|secret)|(?:access|secret|private|auth|signing|encryption)[_-]?key"
    r"|(?:client|app)[_-]?secret|secret|password|passwd|pwd|passphrase)\s*"
    r"""(?:is\s+|[:=：]\s*))(['"]?)(?!\[已脱敏:)([^\s'",;，；。]{4,})\2""",
    _FLAGS,
)
_QUERY = re.compile(
    r"([?&](access_token|refresh_token|id_token|auth_token|token|api_key|apikey|key|secret|auth"
    r"|signature|sig|password|pwd)=)(?!\[已脱敏:)[^&\s#]+",
    _FLAGS,
)
_SECRET_PARAMS = {"api_key", "apikey", "key", "secret", "password", "pwd"}
_OTP_LABEL = r"(?:验证码|校验码|动态码|短信码|安全码|图形码|动态口令)"
_OTP_VALUE = r"(?=[A-Za-z0-9]{4,8}(?![A-Za-z0-9]))(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,8}"
_ENTROPY = re.compile(
    r"(?<![/\w-])(?:(?=[\w-]*[a-z])(?=[\w-]*[A-Z])(?=[\w-]*\d)[\w-]{32,}"
    r"|(?=[0-9a-fA-F]*[a-fA-F])(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{32,})(?![\w-])",
    re.ASCII,
)


def _bank_card(match: re.Match[str]) -> str:
    candidate = match[0]
    digits = re.sub(r"[\s-]", "", candidate)
    size, prefix = len(digits), int(digits[:4])
    recognized = (
        (digits.startswith("4") and size in (13, 16, 19))
        or (size == 16 and (5100 <= prefix <= 5599 or 2221 <= prefix <= 2720))
        or (size == 15 and digits.startswith(("34", "37")))
        or (
            16 <= size <= 19
            and (
                digits.startswith(("62", "6011", "65"))
                or 6440 <= prefix <= 6499
                or 3528 <= prefix <= 3589
            )
        )
    )
    if recognized:
        checksum = 0
        for i, digit in enumerate(reversed(digits)):
            n = int(digit) * (2 if i % 2 else 1)
            checksum += n - 9 if n > 9 else n
        if checksum % 10 == 0:
            return _CARD
    return candidate


def _redact(text: str) -> str:
    if _STANDALONE_CODE.fullmatch(text):
        return _CODE
    text = _KEYS.sub(_SECRET, _JWT.sub(_TOKEN, text))
    text = re.sub(r"(https?://[^/\s:@]+:)[^/\s@]+@", r"\g<1>" + _SECRET + "@", text, flags=_FLAGS)
    text = _QUERY.sub(
        lambda m: m[1] + (_SECRET if m[2].lower() in _SECRET_PARAMS else _TOKEN),
        text,
    )
    text = re.sub(
        r"(\b(?:Bearer|Basic)\s+)[A-Z0-9._~+/=-]{12,}", r"\1" + _TOKEN, text, flags=_FLAGS
    )
    text = _LABELED_TOKEN.sub(r"\1\2" + _TOKEN + r"\2", text)
    text = _LABELED_KEY.sub(r"\1\2" + _SECRET + r"\2", text)
    text = re.sub(
        r"((?:令牌|凭证)\s*(?:是|为|[:=：])?\s*)[a-zA-Z0-9._~+/=-]{8,}", r"\1" + _TOKEN, text
    )
    text = re.sub(
        r"((?:密码|口令|密钥|秘钥|私钥|授权码)\s*(?:是|为|[:=：])?\s*)[a-zA-Z0-9!@#$%^&*_+=~.?/-]{4,}",
        r"\1" + _SECRET,
        text,
    )
    text = re.sub(
        r"((?:银行卡号?|信用卡号?|储蓄卡号?|借记卡号?|卡号|card\s*(?:no\.?|num(?:ber)?)?)"
        r"\s*(?:是|为|[:=：])?\s*)\d(?:[\s-]?\d){12,18}(?!\d)",
        r"\1" + _CARD,
        text,
        flags=_FLAGS,
    )
    text = re.sub(r"(?<!\d)\d(?:[\s-]?\d){12,18}(?!\d)", _bank_card, text, flags=re.ASCII)
    text = re.sub(rf"({_OTP_LABEL}[^a-zA-Z0-9]{{0,6}}?){_OTP_VALUE}", r"\1" + _CODE, text)
    text = re.sub(
        rf"(?<![A-Za-z0-9])\d{{4,8}}(\s*(?:是|为)?\s*(?:我的|你的|本次)?(?:短信|手机|邮箱|登录|动态)?{_OTP_LABEL})",
        _CODE + r"\1",
        text,
    )
    text = re.sub(
        r"(\b(?:verification\s*code|verify\s*code|security\s*code|auth(?:entication)?\s*code"
        rf"|passcode|otp|2fa|mfa|totp)\s*(?:is\s+|[:=：]\s*|\s+)){_OTP_VALUE}",
        r"\1" + _CODE,
        text,
        flags=_FLAGS,
    )
    return _ENTROPY.sub(_TOKEN, text)


def filter_input(row: dict[str, JsonValue]) -> tuple[str, InputAction]:
    raw = row.get("text")
    if not isinstance(raw, str) or not (trimmed := raw.strip()):
        return "", "empty"
    length = row.get("field_length")
    if type(length) is int and length > _MAX_LENGTH:
        return "", "too_long"
    # Masking a four-character code can expand a valid field beyond 500 characters.
    if len(trimmed) > _MAX_LENGTH and (
        len(trimmed) > 4000 or len(_MASKS.sub("x", trimmed)) > _MAX_LENGTH
    ):
        return "", "too_long"
    normalized = _SPACE.sub(" ", trimmed).strip()
    if not normalized:
        return "", "empty"
    protected = _redact(normalized)
    if protected != normalized:
        return protected, "redacted"
    return normalized, "normalized" if normalized != raw else "kept"
