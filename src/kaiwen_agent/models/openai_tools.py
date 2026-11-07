from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from kaiwen_agent.tools.definition import ToolDefinition


def serialize_openai_tool(definition: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "name": openai_tool_name(definition.name),
        "description": definition.description,
        "parameters": strict_openai_schema(definition.input_schema),
        "strict": True,
    }


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
