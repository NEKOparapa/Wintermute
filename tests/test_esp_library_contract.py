import pytest

from purin.agent.tools.connectors.device_arguments import (
    device_tool_info,
    validate_device_arguments,
)


@pytest.mark.parametrize("action", ["read", "delete"])
def test_library_uses_one_exact_shelf_identifier(action: str) -> None:
    for target in ({"id": "walk_1"}, {"file": "雨中的车站.TXT"}):
        validate_device_arguments("esp_library", {"action": action, **target})

    for target in (
        {},
        {"id": "walk_1", "file": "walk.txt"},
        {"file": None},
        {"file": "../walk.txt"},
        {"file": "walk.json"},
        {"file": "雨" * 84 + ".txt"},
    ):
        with pytest.raises(ValueError):
            validate_device_arguments("esp_library", {"action": action, **target})


def test_local_txt_sections_and_pushed_book_chapters_have_separate_limits() -> None:
    validate_device_arguments(
        "esp_library", {"action": "read", "file": "long.txt", "chapter": 1000}
    )
    with pytest.raises(ValueError):
        validate_device_arguments("esp_library", {"action": "read", "id": "long", "chapter": 1000})
    schema = device_tool_info("esp_library").input_schema
    assert "delete" in schema["discriminator"]["mapping"]
