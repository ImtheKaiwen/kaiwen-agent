from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from copy import deepcopy
from typing import Any

from kaiwen_agent.tools.definition import ToolDefinition


def serialize_openai_tool(definition: ToolDefinition) -> dict[str, Any]:
    """The Responses API function tool for one definition.

    Strict mode is chosen per tool. It is kept only when every object in the schema can be
    closed with `additionalProperties: false`; a tool with a free-form mapping field (for
    example `dict[str, str]`) is sent with `strict: false` and its original JSON Schema,
    because forcing it closed would change its meaning. The provider schema is guidance
    only: the host's own argument validation is unchanged either way.
    """

    original = definition.input_schema
    closed = strict_openai_schema(original)
    # Closing objects must not narrow a contract that deliberately allows extra properties,
    # and the schema must use only constructs strict mode documents as supported.
    strict = not declares_open_objects(original) and is_strict_compatible(closed)
    return {
        "type": "function",
        "name": openai_tool_name(definition.name),
        "description": definition.description,
        "parameters": closed if strict else deepcopy(original),
        "strict": strict,
    }


# Keywords OpenAI strict mode documents as unsupported (allOf, not, dependentRequired,
# dependentSchemas, if, then, else), plus keywords reported as rejected or that cannot be
# closed or checked (oneOf, patternProperties, propertyNames, min/maxProperties,
# unevaluatedProperties). Being conservative only ever yields `strict: false`.
_UNSUPPORTED_STRICT_KEYWORDS = frozenset(
    {
        "allOf",
        "not",
        "oneOf",
        "dependentRequired",
        "dependentSchemas",
        "dependencies",
        "if",
        "then",
        "else",
        "patternProperties",
        "propertyNames",
        "minProperties",
        "maxProperties",
        "unevaluatedProperties",
    }
)
_SCHEMA_MAPS = frozenset({"properties", "$defs", "definitions"})
_SCHEMA_VALUES = frozenset(
    {"items", "prefixItems", "anyOf", "oneOf", "allOf", "not", "additionalProperties"}
)


def _schema_nodes(node: Any) -> Iterator[dict[str, Any]]:
    """Every schema node, following schema keywords only (not names, defaults or enums)."""

    if isinstance(node, list):
        for item in node:
            yield from _schema_nodes(item)
        return
    if not isinstance(node, dict):
        return
    yield node
    for key, value in node.items():
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            for child in value.values():
                yield from _schema_nodes(child)
        elif key in _SCHEMA_VALUES:
            yield from _schema_nodes(value)


def _is_object(node: Mapping[str, Any]) -> bool:
    node_type = node.get("type")
    return node_type == "object" or (isinstance(node_type, list) and "object" in node_type)


def declares_open_objects(schema: Mapping[str, Any]) -> bool:
    """Whether the original schema deliberately allows extra properties on an object.

    `additionalProperties: true` or a schema value is an intentional open contract. An absent
    keyword is not: the Pydantic models in this project ignore or forbid extras, and closing
    them is the existing strict behavior.
    """

    return any(
        (_is_object(node) or "properties" in node)
        and "additionalProperties" in node
        and node["additionalProperties"] is not False
        for node in _schema_nodes(schema)
    )


def is_strict_compatible(schema: Mapping[str, Any]) -> bool:
    """Whether a (closed) schema is representable in OpenAI strict mode.

    Every object must be closed with `additionalProperties: false`, and no construct that
    strict mode does not support may appear. A free-form mapping, an object with
    schema-valued `additionalProperties`, or an object with neither `properties` nor a closed
    `additionalProperties` is not representable. This is a conservative gate, not a JSON
    Schema validator.
    """

    for node in _schema_nodes(schema):
        if _UNSUPPORTED_STRICT_KEYWORDS & node.keys():
            return False
        if (_is_object(node) or "properties" in node) and node.get(
            "additionalProperties"
        ) is not False:
            return False
    return True


def openai_tool_name(name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]", "__", name)
    if not normalized or len(normalized) > 64:
        raise ValueError(f"Tool name cannot be represented for OpenAI: {name}")
    return normalized


def strict_openai_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(dict(schema))

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if node.get("type") == "object" and isinstance(properties, dict):
                node["additionalProperties"] = False
                node["required"] = list(properties)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(normalized)
    return normalized
