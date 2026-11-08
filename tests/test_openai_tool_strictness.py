"""Per-tool strict-mode serialization for the OpenAI Responses adapter.

Offline: nothing here talks to a provider. OpenAI strict function schemas require every object
to be closed with `additionalProperties: false`, so a tool with a free-form mapping field is
sent with `strict: false` and its original schema, while strict-compatible tools stay strict.
The provider schema is guidance only; the host validates arguments itself either way.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kaiwen_agent import AgentContext
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.models.openai_tools import (
    declares_open_objects,
    is_strict_compatible,
    serialize_openai_tool,
    strict_openai_schema,
)
from kaiwen_agent.toolkits import (
    CommandCatalog,
    CommandParameter,
    CommandPreset,
    SandboxResult,
)
from kaiwen_agent.toolkits import (
    testing_tools as build_testing_tools,
)
from kaiwen_agent.toolkits.commands import CommandRunInput
from kaiwen_agent.tools.definition import ToolDefinition


def tool(model: type[BaseModel], name: str = "demo.tool") -> ToolDefinition:
    async def handler(context: AgentContext, arguments: BaseModel) -> object:
        return {}

    return ToolDefinition(
        name=name, description="demo", input_model=model, handler=handler, permissions=frozenset()
    )


class Simple(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    count: int = 1


class Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str


class StrictNested(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inner: Inner
    items: list[Inner] = Field(default_factory=list)
    maybe: Inner | None = None


class FreeForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    labels: dict[str, str] = Field(default_factory=dict)


class NestedFreeForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outer: FreeForm


class ArrayOfFreeForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[FreeForm]


class AnyMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]


def test_a_simple_function_stays_strict_and_closed() -> None:
    emitted = serialize_openai_tool(tool(Simple))
    assert emitted["strict"] is True
    assert emitted["parameters"]["additionalProperties"] is False
    assert emitted["parameters"]["required"] == ["name", "count"]


def test_strict_compatible_nested_objects_and_arrays_stay_strict() -> None:
    emitted = serialize_openai_tool(tool(StrictNested))
    assert emitted["strict"] is True
    assert is_strict_compatible(emitted["parameters"])
    assert emitted["parameters"]["$defs"]["Inner"]["additionalProperties"] is False


@pytest.mark.parametrize("model", [FreeForm, NestedFreeForm, ArrayOfFreeForm, AnyMapping])
def test_a_free_form_mapping_anywhere_makes_the_tool_non_strict(model: type[BaseModel]) -> None:
    definition = tool(model)
    emitted = serialize_openai_tool(definition)
    assert emitted["strict"] is False
    # The original schema is sent unchanged: no mapping was turned into an empty-only object,
    # and no `required` rewrite was applied.
    assert emitted["parameters"] == definition.input_schema
    assert not is_strict_compatible(strict_openai_schema(definition.input_schema))


def test_the_free_form_mapping_keeps_its_meaning_in_the_non_strict_schema() -> None:
    schema = serialize_openai_tool(tool(FreeForm))["parameters"]
    assert schema["properties"]["labels"]["additionalProperties"] == {"type": "string"}
    assert schema["required"] == ["name"]  # `labels` stays optional


def test_serialization_never_mutates_a_caller_owned_schema() -> None:
    for model in (Simple, StrictNested, FreeForm, NestedFreeForm):
        definition = tool(model)
        before = copy.deepcopy(definition.input_schema)
        serialize_openai_tool(definition)
        assert definition.input_schema == before
    schema: dict[str, Any] = {"type": "object", "properties": {"a": {"type": "string"}}}
    snapshot = copy.deepcopy(schema)
    strict_openai_schema(schema)
    assert schema == snapshot


def test_every_emitted_definition_has_a_strict_flag_consistent_with_its_schema() -> None:
    for model in (Simple, StrictNested, FreeForm, NestedFreeForm, ArrayOfFreeForm, AnyMapping):
        emitted = serialize_openai_tool(tool(model))
        assert isinstance(emitted["strict"], bool)
        if emitted["strict"]:
            assert is_strict_compatible(emitted["parameters"]), model.__name__
        else:
            assert not is_strict_compatible(strict_openai_schema(emitted["parameters"]))


def test_compatibility_checker_edge_cases() -> None:
    assert is_strict_compatible({"type": "object", "properties": {}, "additionalProperties": False})
    assert not is_strict_compatible({"type": "object"})  # free-form, no properties
    assert not is_strict_compatible({"type": "object", "additionalProperties": True})
    assert not is_strict_compatible(
        {"anyOf": [{"type": "object", "properties": {}}, {"type": "null"}]}
    )
    # A property that is merely *named* like a keyword is not a schema keyword.
    named = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"type": {"type": "string"}, "properties": {"type": "string"}},
    }
    assert is_strict_compatible(named)


# ------------------------------------------------------------------ the real Coding Kit tools


class Runner:
    provides_isolation = True

    def __init__(self, filesystem: WorkspaceFilesystem) -> None:
        self.filesystem = filesystem
        self.root = "main"
        self.commands: list[Any] = []

    async def run(self, command: Any) -> SandboxResult:
        self.commands.append(command)
        return SandboxResult(exit_code=0)


def command_tools(tmp_path: Path) -> tuple[tuple[ToolDefinition, ...], CommandCatalog, Runner]:
    root = tmp_path / "project"
    root.mkdir()
    filesystem = WorkspaceFilesystem(workspace_id="w", roots={"main": root})
    runner = Runner(filesystem)
    catalog = CommandCatalog(
        filesystem=filesystem,
        root="main",
        runner=runner,
        presets=(
            CommandPreset(id="unit.tests", category="test", executable="python", arguments=("-V",)),
            CommandPreset(
                id="unit.one",
                category="test",
                executable="python",
                arguments=("-m", "unittest", "{target}"),
                parameters=(CommandParameter(name="target"),),
            ),
        ),
    )
    return build_testing_tools(catalog, unattended_categories=frozenset({"test"})), catalog, runner


def test_the_real_coding_kit_command_tools_are_non_strict_with_the_original_schema(
    tmp_path: Path,
) -> None:
    tools, _, _ = command_tools(tmp_path)
    assert {item.name for item in tools} >= {"testing.run", "quality.lint", "build.run"}
    for definition in tools:
        emitted = serialize_openai_tool(definition)
        assert emitted["strict"] is False, definition.name
        assert emitted["parameters"] == CommandRunInput.model_json_schema()
        properties = emitted["parameters"]["properties"]
        assert properties["parameters"]["additionalProperties"] == {"type": "string"}
        assert emitted["parameters"]["required"] == ["command_id"]


def test_the_probe_reports_the_serialized_tools_without_a_request() -> None:
    from scripts import live_validation as lv

    assert lv.probe_openai_tool_schemas() == []
    strictness = lv.probe_openai_tool_strictness()
    assert strictness["testing__run"] is False and set(strictness.values()) == {False}


# --------------------------------------------------- host validation does not depend on strict


def test_host_side_argument_validation_is_unchanged_by_the_provider_schema(
    tmp_path: Path,
) -> None:
    tools, catalog, runner = command_tools(tmp_path)
    run_tool = next(item for item in tools if item.name == "testing.run")
    model = run_tool.input_model
    # Unknown fields, a missing command id, and wrong value types are rejected by Pydantic.
    for bad in (
        {"command_id": "unit.tests", "parameters": {}, "extra": "x"},
        {"parameters": {}},
        {"command_id": "", "parameters": {}},
        {"command_id": "unit.tests", "parameters": {"k": 1}},
        {"command_id": "unit.tests", "parameters": ["a"]},
    ):
        with pytest.raises(ValidationError):
            model.model_validate(bad)
    # The default still applies: the non-strict schema leaves `parameters` optional.
    assert CommandRunInput.model_validate({"command_id": "unit.tests"}).parameters == {}

    context = AgentContext(
        session_id="s",
        tenant_id="t",
        user_id="u",
        metadata={"runtime_run_id": "r", "workspace_id": "w"},
    )

    def execute(arguments: dict[str, Any], category: str = "test") -> Any:
        parsed = CommandRunInput.model_validate(arguments)
        return asyncio.run(catalog.execute(context, parsed, category=category))

    # Unknown commands, missing or unexpected parameters and a wrong category never run.
    with pytest.raises(Exception):  # noqa: B017 - the exact error type is host-internal
        execute({"command_id": "not.registered"})
    with pytest.raises(ValueError, match="exactly match"):
        execute({"command_id": "unit.one"})
    with pytest.raises(ValueError, match="exactly match"):
        execute({"command_id": "unit.tests", "parameters": {"target": "x"}})
    with pytest.raises(ValueError, match="invalid"):
        execute({"command_id": "unit.one", "parameters": {"target": "../escape"}})
    with pytest.raises(Exception):  # noqa: B017
        execute({"command_id": "unit.tests"}, category="lint")
    assert runner.commands == []


# --------------------------------------- unsupported composition and intentional open objects


def schema_tool(schema: dict[str, Any]) -> ToolDefinition:
    """A tool whose JSON Schema is exactly `schema`."""

    class Fixed(BaseModel):
        model_config = ConfigDict(extra="forbid")

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return copy.deepcopy(schema)

    return tool(Fixed)


def closed_object(**properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in properties}
        or {"a": {"type": "string"}},
        "additionalProperties": False,
    }


@pytest.mark.parametrize(
    "keywords",
    [
        {"allOf": [closed_object()]},
        {"not": {"type": "null"}},
        {"oneOf": [closed_object(), {"type": "null"}]},
        {"if": {"type": "object"}, "then": {}, "else": {}},
        {"dependentRequired": {"a": ["b"]}},
        {"dependentSchemas": {"a": {}}},
        {"patternProperties": {"^x": {"type": "string"}}},
        {"propertyNames": {"pattern": "^a"}},
        {"minProperties": 1},
        {"unevaluatedProperties": False},
    ],
)
def test_unsupported_composition_keywords_never_produce_strict_true(
    keywords: dict[str, Any],
) -> None:
    schema = {**closed_object(), **keywords}
    emitted = serialize_openai_tool(schema_tool(schema))
    assert emitted["strict"] is False
    assert emitted["parameters"] == schema  # the original schema is preserved unchanged
    # The same construct nested below a property or inside an array is also caught.
    nested = {
        **closed_object(),
        "properties": {"deep": {"type": "array", "items": {**closed_object(), **keywords}}},
    }
    assert serialize_openai_tool(schema_tool(nested))["strict"] is False


def test_any_of_with_closed_objects_stays_strict() -> None:
    schema = {
        **closed_object(),
        "properties": {"choice": {"anyOf": [closed_object(), {"type": "null"}]}},
    }
    emitted = serialize_openai_tool(schema_tool(schema))
    assert emitted["strict"] is True and is_strict_compatible(emitted["parameters"])


def test_property_names_that_look_like_keywords_are_not_keywords() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "not": {"type": "string"},
            "allOf": {"type": "string"},
            "oneOf": {"type": "string"},
            "default": {"type": "string", "default": "allOf"},
        },
    }
    assert serialize_openai_tool(schema_tool(schema))["strict"] is True


class AllowsExtras(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str


class TypedExtras(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    __pydantic_extra__: dict[str, int] = Field(init=False)


class ClosedWithOpenChild(BaseModel):
    model_config = ConfigDict(extra="forbid")

    child: AllowsExtras


def test_an_intentional_open_object_contract_is_not_narrowed() -> None:
    for model in (AllowsExtras, TypedExtras, ClosedWithOpenChild):
        definition = tool(model)
        original = definition.input_schema
        emitted = serialize_openai_tool(definition)
        assert emitted["strict"] is False, model.__name__
        assert emitted["parameters"] == original, model.__name__
    assert serialize_openai_tool(tool(AllowsExtras))["parameters"]["additionalProperties"] is True


def test_raw_schema_open_object_forms() -> None:
    explicit_true = {**closed_object(), "additionalProperties": True}
    schema_valued = {**closed_object(), "additionalProperties": {"type": "string"}}
    for schema in (explicit_true, schema_valued):
        emitted = serialize_openai_tool(schema_tool(schema))
        assert emitted["strict"] is False and emitted["parameters"] == schema
    # Absent additionalProperties is the default Pydantic shape and is closed, as before.
    absent = {"type": "object", "properties": {"a": {"type": "string"}}}
    emitted = serialize_openai_tool(schema_tool(absent))
    assert emitted["strict"] is True and emitted["parameters"]["additionalProperties"] is False
    assert declares_open_objects(explicit_true) and declares_open_objects(schema_valued)
    assert not declares_open_objects(absent) and not declares_open_objects(closed_object())


def test_compatible_closed_nested_objects_are_unchanged_by_the_new_checks() -> None:
    for model in (Simple, StrictNested):
        definition = tool(model)
        emitted = serialize_openai_tool(definition)
        assert emitted["strict"] is True
        assert emitted["parameters"] == strict_openai_schema(definition.input_schema)


def test_the_realtime_provider_registers_tools_through_the_same_serializer() -> None:
    from kaiwen_agent.realtime import (
        OpenAILiveProviderOptions,
        OpenAILiveWebRTCService,
    )
    from tests.test_openai_live_provider import FakeLiveHTTPTransport, live_config

    free_form = tool(FreeForm, "demo.freeform")
    strict_tool = tool(Simple, "demo.simple")

    async def scenario() -> list[dict[str, Any]]:
        transport = FakeLiveHTTPTransport(
            {"session": {"id": "live"}, "transport": {"type": "webrtc", "sdp": "answer"}}
        )
        service = OpenAILiveWebRTCService(api_key="server-secret", transport=transport)
        await service.create_session(
            sdp_offer="offer",
            config=live_config(),
            options=OpenAILiveProviderOptions(response_model="m", response_instructions="i"),
            tools=[free_form, strict_tool],
        )
        tools: list[dict[str, Any]] = transport.body["session"]["delegation"]["responses"]["tools"]
        return tools

    sent = asyncio.run(scenario())
    assert sent == [serialize_openai_tool(free_form), serialize_openai_tool(strict_tool)]
    assert [item["strict"] for item in sent] == [False, True]
    assert sent[0]["parameters"] == free_form.input_schema
