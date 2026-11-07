from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.context import AgentContext
from kaiwen_agent.tools.definition import ToolDefinition


class MCPClient(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class MCPToolDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    description: str = Field(default="", max_length=2_000)
    input_schema: dict[str, Any]


class MCPToolBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    descriptor: MCPToolDescriptor
    input_model: type[BaseModel]
    permissions: frozenset[str] = Field(min_length=1)
    side_effect: Literal["none", "reversible", "destructive"] = "none"
    requires_approval: bool = False


def bind_mcp_tool(client: MCPClient, binding: MCPToolBinding) -> ToolDefinition:
    expected = _canonical_schema(binding.input_model.model_json_schema())
    discovered = _canonical_schema(binding.descriptor.input_schema)
    if expected != discovered:
        raise ValueError("MCP descriptor schema does not match the trusted input model")
    if binding.side_effect != "none" and not binding.requires_approval:
        raise ValueError("Side-effecting MCP tools must require approval")

    async def execute(context: AgentContext, arguments: BaseModel) -> Any:
        del context
        return await client.call_tool(
            binding.descriptor.name,
            arguments.model_dump(mode="json"),
        )

    return ToolDefinition(
        name=f"mcp.{binding.descriptor.name}",
        description=binding.descriptor.description,
        input_model=binding.input_model,
        handler=execute,
        permissions=binding.permissions,
        side_effect=binding.side_effect,
        requires_approval=binding.requires_approval,
    )


def _canonical_schema(schema: dict[str, Any]) -> str:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False)
