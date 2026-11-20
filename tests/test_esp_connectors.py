import pytest

from wintermute.agent.tools.connectors.device_arguments import (
    device_tool_info,
    validate_device_arguments,
)


@pytest.mark.parametrize(
    ("tool", "examples"),
    [
        (
            "esp_vibration",
            [{"action": "pulse", "duration_ms": 1000}, {"action": "stop"}, {"action": "status"}],
        ),
        (
            "esp_player",
            [
                {"action": "play"},
                {"action": "play", "file": "Track 01.WAV"},
                {"action": "list", "offset": 4},
                {"action": "stop"},
                {"action": "status"},
            ],
        ),
        (
            "esp_card",
            [
                {"action": "show", "title": "提醒", "body": "会议要点\n带上笔记本"},
                {"action": "status"},
            ],
        ),
        (
            "esp_timer",
            [{"action": "start", "seconds": 86400}, {"action": "stop"}, {"action": "status"}],
        ),
        (
            "esp_library",
            [
                {"action": "add", "id": "book-1", "title": "短篇", "text": "书" * 2500},
                {
                    "action": "append",
                    "id": "book-1",
                    "chapter": 2,
                    "title": "第二章",
                    "text": "继续",
                },
                {"action": "list"},
                {"action": "read", "id": "book-1", "chapter": 2, "page": 1},
            ],
        ),
    ],
)
def test_esp_connector_actions_match_advertised_schema(tool, examples):
    schema = device_tool_info(tool).input_schema
    assert set(schema["discriminator"]["mapping"]) == {args["action"] for args in examples}
    for args in examples:
        validate_device_arguments(tool, args)
        with pytest.raises(ValueError):
            validate_device_arguments(tool, {**args, "unexpected": True})


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("esp_vibration", {"action": "pulse", "duration_ms": 1001}),
        ("esp_player", {"action": "play", "file": "../secret.wav"}),
        ("esp_card", {"action": "show", "title": "提醒", "body": "a\x00b"}),
        ("esp_timer", {"action": "start", "seconds": "5"}),
        ("esp_library", {"action": "read", "id": "../book"}),
        ("esp_library", {"action": "append", "id": "b", "chapter": 1, "title": "章", "text": "续"}),
        ("esp_library", {"action": "add", "id": "b", "title": "书", "text": "字" * 2501}),
    ],
)
def test_esp_connector_rejects_invalid_requests(tool, args):
    with pytest.raises(ValueError):
        validate_device_arguments(tool, args)
