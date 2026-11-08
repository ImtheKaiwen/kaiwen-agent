from __future__ import annotations

import re
from collections.abc import Mapping
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
    strict = is_strict_compatible(closed)
    return {
        "type": "function",
        "name": openai_tool_name(definition.name),
        "description": definition.description,
        "parameters": closed if strict else deepcopy(original),
        "strict": strict,
    }


def is_strict_compatible(schema: Mapping[str, Any]) -> bool:
    """Whether every object in the schema is closed (`additionalProperties` is false).

    OpenAI strict function schemas require this of every object. A free-form mapping, an
    object with schema-valued `additionalProperties`, or an object with neither `properties`
    nor a closed `additionalProperties` is not representable.
    """

    def closed(node: Any) -> bool:
        if isinstance(node, list):
            return all(closed(item) for item in node)
        if not isinstance(node, dict):
            return True
        node_type = node.get("type")
        is_object = node_type == "object" or (isinstance(node_type, list) and "object" in node_type)
        if (is_object or "properties" in node) and node.get("additionalProperties") is not False:
            return False
        for key, value in node.items():
            if key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
                children: list[Any] = list(value.values())
            elif key in {"items", "prefixItems", "anyOf", "oneOf", "allOf", "not"}:
                children = [value]
            else:
                continue
            if not all(closed(child) for child in children):
                return False
        return True

    return closed(schema)


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
