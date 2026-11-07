from __future__ import annotations

import re
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.context import AgentContext
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.tools.definition import ToolDefinition

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


class PatchConflictError(RuntimeError):
    pass


class PatchFormatError(ValueError):
    pass


class WorkspacePatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=2_000)
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    patch: str = Field(min_length=1, max_length=1_000_000)


class WorkspaceMkdirInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=2_000)


def workspace_write_tools(
    filesystem: WorkspaceFilesystem,
    *,
    max_patch_bytes: int = 250_000,
) -> tuple[ToolDefinition, ...]:
    if max_patch_bytes < 1:
        raise ValueError("max_patch_bytes must be positive")

    async def apply_patch(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, WorkspacePatchInput):
            raise TypeError("Workspace patch arguments were not validated")
        filesystem.require_context(context)
        if len(arguments.patch.encode()) > max_patch_bytes:
            raise ValueError("Workspace patch exceeds the configured byte limit")
        original = filesystem.read_text(arguments.root, arguments.path)
        current_hash = sha256(original.encode()).hexdigest()
        if current_hash != arguments.expected_sha256:
            raise PatchConflictError("Workspace patch preimage checksum is stale")
        updated = apply_unified_patch(original, arguments.patch)
        bytes_written = filesystem.write_text(arguments.root, arguments.path, updated)
        return {
            "path": arguments.path,
            "previous_sha256": current_hash,
            "sha256": sha256(updated.encode()).hexdigest(),
            "bytes_written": bytes_written,
        }

    async def mkdir(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, WorkspaceMkdirInput):
            raise TypeError("Workspace mkdir arguments were not validated")
        filesystem.require_context(context)
        return {"path": filesystem.mkdir(arguments.root, arguments.path)}

    return (
        ToolDefinition(
            name="workspace.apply_patch",
            description=("Apply one bounded unified diff after verifying the exact file checksum."),
            input_model=WorkspacePatchInput,
            handler=apply_patch,
            permissions=frozenset({"workspace.write"}),
            side_effect="reversible",
            requires_approval=True,
        ),
        ToolDefinition(
            name="workspace.mkdir",
            description="Create one directory under an existing allowlisted workspace parent.",
            input_model=WorkspaceMkdirInput,
            handler=mkdir,
            permissions=frozenset({"workspace.write"}),
            side_effect="reversible",
            requires_approval=True,
        ),
    )


def apply_unified_patch(original: str, patch: str) -> str:
    """Apply a strict, single-file unified diff without invoking a patch process."""

    lines = patch.splitlines()
    if len(lines) < 3 or not lines[0].startswith("--- ") or not lines[1].startswith("+++ "):
        raise PatchFormatError("Patch must begin with one unified diff file header")
    if any(line.startswith(("--- ", "+++ ")) for line in lines[2:]):
        raise PatchFormatError("Patch may modify exactly one file")
    source = original.splitlines()
    output: list[str] = []
    source_index = 0
    patch_index = 2
    while patch_index < len(lines):
        match = _HUNK.fullmatch(lines[patch_index])
        if match is None:
            raise PatchFormatError("Patch contains content outside a valid hunk")
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_count = int(match.group(4) or "1")
        target_index = old_start - 1
        if target_index < source_index or target_index > len(source):
            raise PatchConflictError("Patch hunk position does not match the current file")
        output.extend(source[source_index:target_index])
        source_index = target_index
        patch_index += 1
        consumed = 0
        produced = 0
        while patch_index < len(lines) and not lines[patch_index].startswith("@@ "):
            line = lines[patch_index]
            patch_index += 1
            if line == r"\ No newline at end of file":
                continue
            if not line or line[0] not in {" ", "+", "-"}:
                raise PatchFormatError("Patch hunk line has an invalid prefix")
            marker, value = line[0], line[1:]
            if marker in {" ", "-"}:
                if source_index >= len(source) or source[source_index] != value:
                    raise PatchConflictError("Patch context does not match the current file")
                source_index += 1
                consumed += 1
            if marker in {" ", "+"}:
                output.append(value)
                produced += 1
        if consumed != old_count or produced != new_count:
            raise PatchFormatError("Patch hunk counts do not match its content")
    output.extend(source[source_index:])
    trailing_newline = original.endswith(("\n", "\r"))
    result = "\n".join(output)
    return result + ("\n" if trailing_newline else "")
