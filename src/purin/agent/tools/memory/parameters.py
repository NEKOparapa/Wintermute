"""Validated action models and the model-facing schemas for memory tools."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, GetJsonSchemaHandler, RootModel
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListMemory(_Arguments):
    action: Literal["list"]
    path: str = ""
    recursive: bool = True
    max_depth: int = Field(default=3, ge=1, le=5)


class ReadMemory(_Arguments):
    action: Literal["read"]
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class SearchMemory(_Arguments):
    action: Literal["search"]
    query: str = Field(min_length=1, max_length=200)
    scope: Literal["all", "agent-character", "about-user", "note", "log"] = "all"
    regex: bool = False
    include_archived: bool = True
    max_results: int = Field(default=20, ge=1, le=50)
    context_lines: int = Field(default=2, ge=0, le=5)


class _FlatToolSchema:
    """Publish a provider-compatible object while retaining strict action validation."""

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler, /
    ) -> JsonSchemaValue:
        schema = handler(core_schema)
        variants = [handler.resolve_ref_schema(branch) for branch in schema["oneOf"]]
        fields: dict[str, list[tuple[str, JsonSchemaValue, bool]]] = {}
        actions: list[str] = []
        required: set[str] | None = None
        for variant in variants:
            action = str(variant["properties"]["action"]["const"])
            actions.append(action)
            action_required = set(variant.get("required", []))
            required = action_required if required is None else required & action_required
            for name, value in variant["properties"].items():
                fields.setdefault(name, []).append((action, value, name in action_required))

        properties: dict[str, JsonSchemaValue] = {
            "action": {"type": "string", "enum": actions, "description": "Memory operation"}
        }
        for name, occurrences in fields.items():
            if name == "action":
                continue
            # Keep constraints shared by all actions using this field. For example, create
            # permits empty content while insert does not; local union validation enforces it.
            first = occurrences[0][1]
            field = {
                key: value
                for key, value in first.items()
                if key != "description"
                and all(key in other and other[key] == value for _, other, _ in occurrences)
            }
            used_by = ", ".join(action for action, _, _ in occurrences)
            required_by = ", ".join(action for action, _, needed in occurrences if needed)
            description = f"Used only with action: {used_by}."
            if required_by:
                description += f" Required with action: {required_by}."
            if original := first.get("description"):
                description += f" {original}"
            field["description"] = description
            properties[name] = field
        return {
            "title": schema.get("title", cls.__name__),
            "type": "object",
            "description": "Choose one action and supply only that action's parameters.",
            "properties": properties,
            "required": sorted(required or {"action"}),
            "additionalProperties": False,
        }


class MemoryReadInput(
    _FlatToolSchema,
    RootModel[Annotated[ListMemory | ReadMemory | SearchMemory, Field(discriminator="action")]],
):
    pass


class _Write(_Arguments):
    path: str
    reason: str = Field(min_length=2, max_length=300)
    expected_hash: str | None = None


class CreateMemory(_Write):
    action: Literal["create"]
    content: str


class ReplaceMemory(_Write):
    action: Literal["replace"]
    old_text: str = Field(min_length=1)
    new_text: str


class InsertMemory(_Write):
    action: Literal["insert"]
    content: str = Field(min_length=1)
    position: Literal["append", "prepend", "after_heading", "after_text"] = "append"
    anchor: str | None = None


class DeleteMemory(_Write):
    action: Literal["delete"]


class RenameMemory(_Write):
    action: Literal["rename"]
    new_path: str


class MemoryWriteInput(
    _FlatToolSchema,
    RootModel[
        Annotated[
            CreateMemory | ReplaceMemory | InsertMemory | DeleteMemory | RenameMemory,
            Field(discriminator="action"),
        ]
    ],
):
    pass
