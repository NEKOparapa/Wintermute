"""Read the source and time footer from the actual user message sent to the model."""


def event_fields(text: str) -> dict[str, str]:
    body, separator, footer = text.rpartition("\n\n---\n")
    origin, newline, timestamp = footer.partition("\n")
    if (
        not separator
        or not newline
        or not origin.startswith("来源：")
        or not timestamp.startswith("时间：")
        or "\n" in timestamp
    ):
        return {}
    return {
        "正文": body,
        "来源": origin.removeprefix("来源："),
        "时间": timestamp.removeprefix("时间："),
    }
