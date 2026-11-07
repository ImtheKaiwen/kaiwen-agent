from __future__ import annotations

from collections.abc import Sequence

from kaiwen_agent.models.openai_tools import openai_tool_name
from kaiwen_agent.tools.definition import ToolDefinition


def provider_tool_catalog(
    tools: Sequence[ToolDefinition],
) -> tuple[list[dict[str, object]], dict[str, str]]:
    names = {openai_tool_name(tool.name): tool.name for tool in tools}
    if len(names) != len(tools):
        raise ValueError("Tool names collide after provider-compatible normalization")
    catalog: list[dict[str, object]] = [
        {
            "type": "function",
            "function": {
                "name": openai_tool_name(tool.name),
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]
    return catalog, names
