"""手机应用前台停留的过滤规则。"""

from typing import Literal

from purin.events.core.activity import MOBILE_USAGE_STATE_KEY

MOBILE_SHORT_DWELL_SECONDS = 8


def mobile_usage_filter_reason(
    state_key: object, value: object
) -> Literal["mobile_home", "mobile_zero_duration", "mobile_short_dwell"] | None:
    if state_key != MOBILE_USAGE_STATE_KEY or not isinstance(value, dict):
        return None
    if value.get("is_home_app") is True:
        return "mobile_home"
    seconds = value.get("duration_seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        if 0 <= seconds < 1:
            return "mobile_zero_duration"
        if 1 <= seconds <= MOBILE_SHORT_DWELL_SECONDS:
            return "mobile_short_dwell"
    return None
