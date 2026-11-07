from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.context import AgentContext
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.tools.definition import ToolDefinition


class WorkspaceSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str = Field(min_length=1, max_length=100)
    path: str = Field(default=".", min_length=1, max_length=2_000)
    query: str = Field(min_length=1, max_length=500)
    regex: bool = False
    case_sensitive: bool = False
    max_results: int = Field(default=100, ge=1, le=500)
    max_files: int = Field(default=1_000, ge=1, le=5_000)


def workspace_search_tool(filesystem: WorkspaceFilesystem) -> ToolDefinition:
    async def search(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, WorkspaceSearchInput):
            raise TypeError("Workspace search arguments were not validated")
        filesystem.require_context(context)
        return filesystem.search_text(
            arguments.root,
            arguments.path,
            arguments.query,
            regex=arguments.regex,
            case_sensitive=arguments.case_sensitive,
            max_results=arguments.max_results,
            max_files=arguments.max_files,
        )

    return ToolDefinition(
        name="workspace.search_text",
        description=(
            "Search bounded UTF-8 workspace files while excluding secrets and generated caches."
        ),
        input_model=WorkspaceSearchInput,
        handler=search,
        permissions=frozenset({"workspace.read"}),
    )
