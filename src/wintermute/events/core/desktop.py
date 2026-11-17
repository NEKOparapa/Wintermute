"""Desktop foreground visit observations."""

WINDOW_SPAN_STATE_KEY = "desktop.window_span"


def compact_window_title(title: str, app: object) -> str:
    """Remove Chrome's redundant window-title suffix from rendered activity text."""
    if isinstance(app, str) and app.strip().casefold() in {
        "chrome", "chrome.exe", "google chrome", "google chrome.app", "com.google.chrome",
    }:
        return title.rstrip().removesuffix(" - Google Chrome") or title
    return title
