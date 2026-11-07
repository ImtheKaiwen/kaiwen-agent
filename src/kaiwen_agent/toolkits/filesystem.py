from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.context import AgentContext
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.tools.definition import ToolDefinition


class WorkspaceListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str = Field(min_length=1, max_length=100)
    path: str = Field(default=".", min_length=1, max_length=2_000)
    max_depth: int = Field(default=4, ge=0, le=20)
    max_results: int = Field(default=500, ge=1, le=2_000)


class WorkspaceStatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=2_000)


class WorkspaceReadLinesInput(WorkspaceStatInput):
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)


class WorkspaceBatchItem(WorkspaceReadLinesInput):
    pass


class WorkspaceReadBatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: tuple[WorkspaceBatchItem, ...] = Field(min_length=1, max_length=20)


def workspace_read_tools(filesystem: WorkspaceFilesystem) -> tuple[ToolDefinition, ...]:
    """Build bounded read-only tools around one trusted workspace binding."""

    async def list_files(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, WorkspaceListInput):
            raise TypeError("Workspace listing arguments were not validated")
        filesystem.require_context(context)
        return filesystem.list_files(
            arguments.root,
            arguments.path,
            max_depth=arguments.max_depth,
            max_results=arguments.max_results,
        )

    async def stat(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, WorkspaceStatInput):
            raise TypeError("Workspace stat arguments were not validated")
        filesystem.require_context(context)
        return filesystem.stat(arguments.root, arguments.path)

    async def read_lines(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, WorkspaceReadLinesInput):
            raise TypeError("Workspace line arguments were not validated")
        if arguments.end_line - arguments.start_line + 1 > 1_000:
            raise ValueError("Workspace line request exceeds 1,000 lines")
        filesystem.require_context(context)
        return filesystem.read_lines(
            arguments.root,
            arguments.path,
            start_line=arguments.start_line,
            end_line=arguments.end_line,
        )

    async def read_batch(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, WorkspaceReadBatchInput):
            raise TypeError("Workspace batch arguments were not validated")
        filesystem.require_context(context)
        results: list[object] = []
        for item in arguments.files:
            if item.end_line - item.start_line + 1 > 1_000:
                raise ValueError("Workspace batch item exceeds 1,000 lines")
            results.append(
                filesystem.read_lines(
                    item.root,
                    item.path,
                    start_line=item.start_line,
                    end_line=item.end_line,
                )
            )
        return {"files": results}

    permission = frozenset({"workspace.read"})
    return (
        ToolDefinition(
            name="workspace.list_files",
            description="List bounded entries inside an allowlisted workspace root.",
            input_model=WorkspaceListInput,
            handler=list_files,
            permissions=permission,
        ),
        ToolDefinition(
            name="workspace.stat",
            description="Read bounded metadata and a checksum for one workspace path.",
            input_model=WorkspaceStatInput,
            handler=stat,
            permissions=permission,
        ),
        ToolDefinition(
            name="workspace.read_lines",
            description="Read a bounded UTF-8 line range from an allowlisted workspace file.",
            input_model=WorkspaceReadLinesInput,
            handler=read_lines,
            permissions=permission,
        ),
        ToolDefinition(
            name="workspace.read_batch",
            description="Read bounded UTF-8 ranges from at most twenty workspace files.",
            input_model=WorkspaceReadBatchInput,
            handler=read_batch,
            permissions=permission,
        ),
    )
