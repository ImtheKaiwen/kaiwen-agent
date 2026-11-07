from __future__ import annotations

import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.context import AgentContext
from kaiwen_agent.tools.definition import ToolDefinition


class WorkspacePathError(PermissionError):
    pass


class WorkspaceFilesystem:
    """Bounded filesystem adapter for one host-authorized workspace."""

    def __init__(
        self,
        *,
        workspace_id: str,
        roots: dict[str, Path],
        max_read_bytes: int = 1_000_000,
        max_write_bytes: int = 1_000_000,
        tenant_id: str | None = None,
        user_id: str | None = None,
        ignored_names: frozenset[str] | None = None,
    ) -> None:
        if not workspace_id or not roots:
            raise ValueError("workspace_id and roots are required")
        if any(not name for name in roots):
            raise ValueError("Workspace root names cannot be empty")
        self.workspace_id = workspace_id
        self.roots = {name: path.resolve(strict=True) for name, path in roots.items()}
        if any(not path.is_dir() for path in self.roots.values()):
            raise ValueError("Workspace roots must be directories")
        if max_read_bytes < 1 or max_write_bytes < 1:
            raise ValueError("Filesystem limits must be positive")
        self.max_read_bytes = max_read_bytes
        self.max_write_bytes = max_write_bytes
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.ignored_names = ignored_names or frozenset(
            {
                ".env",
                ".git",
                ".mypy_cache",
                ".next",
                ".pytest_cache",
                ".ruff_cache",
                ".venv",
                "__pycache__",
                "build",
                "dist",
                "node_modules",
                "venv",
            }
        )

    def read_text(self, root: str, relative_path: str) -> str:
        path = self._resolve(root, relative_path, must_exist=True)
        if not path.is_file():
            raise WorkspacePathError("Workspace path is not a file")
        if path.stat().st_size > self.max_read_bytes:
            raise ValueError("Workspace read exceeds the configured byte limit")
        return path.read_text(encoding="utf-8")

    def require_context(self, context: AgentContext) -> None:
        """Validate the trusted runtime identity bound by the host."""

        _require_workspace(context, self)

    def write_text(self, root: str, relative_path: str, content: str) -> int:
        encoded = content.encode()
        if len(encoded) > self.max_write_bytes:
            raise ValueError("Workspace write exceeds the configured byte limit")
        path = self._resolve(root, relative_path, must_exist=False)
        if not path.parent.is_dir():
            raise WorkspacePathError("Workspace parent directory does not exist")
        handle, temporary = tempfile.mkstemp(prefix=".kaiwen-", dir=path.parent)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return len(encoded)

    def mkdir(self, root: str, relative_path: str) -> str:
        path = self._resolve(root, relative_path, must_exist=False)
        self._reject_ignored(path)
        if path.exists():
            raise FileExistsError("Workspace directory already exists")
        if not path.parent.is_dir():
            raise WorkspacePathError("Workspace parent directory does not exist")
        path.mkdir()
        return self._relative(root, path)

    def stat(self, root: str, relative_path: str) -> dict[str, Any]:
        path = self._resolve(root, relative_path, must_exist=True)
        self._reject_ignored(path)
        result: dict[str, Any] = {
            "path": self._relative(root, path),
            "type": "directory" if path.is_dir() else "file" if path.is_file() else "other",
            "size": path.stat().st_size,
            "sha256": None,
        }
        if path.is_file():
            if path.stat().st_size > self.max_read_bytes:
                raise ValueError("Workspace stat checksum exceeds the configured byte limit")
            result["sha256"] = sha256(path.read_bytes()).hexdigest()
        return result

    def read_lines(
        self,
        root: str,
        relative_path: str,
        *,
        start_line: int,
        end_line: int,
    ) -> dict[str, Any]:
        if start_line < 1 or end_line < start_line:
            raise ValueError("Invalid workspace line range")
        self._reject_ignored(self._resolve(root, relative_path, must_exist=True))
        lines = self.read_text(root, relative_path).splitlines()
        selected = lines[start_line - 1 : end_line]
        return {
            "path": relative_path,
            "start_line": start_line,
            "end_line": min(end_line, len(lines)),
            "total_lines": len(lines),
            "content": "\n".join(selected),
        }

    def list_files(
        self,
        root: str,
        relative_path: str = ".",
        *,
        max_depth: int = 4,
        max_results: int = 500,
    ) -> dict[str, Any]:
        if max_depth < 0 or max_results < 1:
            raise ValueError("Workspace listing limits are invalid")
        start = self._resolve(root, relative_path, must_exist=True)
        self._reject_ignored(start)
        if not start.is_dir():
            raise WorkspacePathError("Workspace listing path is not a directory")
        entries: list[dict[str, Any]] = []
        truncated = False
        stack: list[tuple[Path, int]] = [(start, 0)]
        while stack:
            directory, depth = stack.pop()
            children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
            nested: list[Path] = []
            for child in children:
                if self._is_ignored(child):
                    continue
                resolved = child.resolve(strict=False)
                if not self._within_root(root, resolved):
                    continue
                is_directory = resolved.is_dir() and not child.is_symlink()
                entries.append(
                    {
                        "path": self._relative(root, child),
                        "type": "directory" if is_directory else "file",
                        "size": None if is_directory else resolved.stat().st_size,
                    }
                )
                if len(entries) >= max_results:
                    truncated = True
                    break
                if is_directory and depth < max_depth:
                    nested.append(resolved)
            if truncated:
                break
            stack.extend((item, depth + 1) for item in reversed(nested))
        return {"entries": entries, "truncated": truncated}

    def search_text(
        self,
        root: str,
        relative_path: str,
        query: str,
        *,
        regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
        max_files: int = 1_000,
    ) -> dict[str, Any]:
        if not query or len(query) > 500:
            raise ValueError("Search query must contain between 1 and 500 characters")
        if max_results < 1 or max_files < 1:
            raise ValueError("Workspace search limits are invalid")
        matcher = _search_matcher(query, regex=regex, case_sensitive=case_sensitive)
        listing = self.list_files(
            root,
            relative_path,
            max_depth=20,
            max_results=max_files,
        )
        matches: list[dict[str, Any]] = []
        searched_files = 0
        skipped_files = 0
        for entry in listing["entries"]:
            if entry["type"] != "file":
                continue
            path = str(entry["path"])
            if self._is_sensitive_path(Path(path)):
                skipped_files += 1
                continue
            size = entry["size"]
            if not isinstance(size, int) or size > self.max_read_bytes:
                skipped_files += 1
                continue
            try:
                content = self.read_text(root, path)
            except (UnicodeDecodeError, OSError):
                skipped_files += 1
                continue
            searched_files += 1
            for line_number, line in enumerate(content.splitlines(), start=1):
                if matcher(line):
                    matches.append(
                        {
                            "path": path,
                            "line": line_number,
                            "preview": line[:500],
                        }
                    )
                    if len(matches) >= max_results:
                        return {
                            "matches": matches,
                            "truncated": True,
                            "searched_files": searched_files,
                            "skipped_files": skipped_files,
                        }
        return {
            "matches": matches,
            "truncated": bool(listing["truncated"]),
            "searched_files": searched_files,
            "skipped_files": skipped_files,
        }

    def _resolve(self, root: str, relative_path: str, *, must_exist: bool) -> Path:
        try:
            base = self.roots[root]
        except KeyError as error:
            raise WorkspacePathError("Workspace root is not allowlisted") from error
        supplied = Path(relative_path)
        if supplied.is_absolute() or not relative_path or "\x00" in relative_path:
            raise WorkspacePathError("Workspace path must be relative")
        try:
            candidate = (base / supplied).resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise WorkspacePathError("Workspace path cannot be resolved") from error
        if not candidate.is_relative_to(base):
            raise WorkspacePathError("Workspace path escapes its allowlisted root")
        if must_exist and not candidate.exists():
            raise WorkspacePathError("Workspace path does not exist")
        return candidate

    def _within_root(self, root: str, path: Path) -> bool:
        return path.is_relative_to(self.roots[root])

    def _relative(self, root: str, path: Path) -> str:
        return path.resolve(strict=False).relative_to(self.roots[root]).as_posix() or "."

    def _is_ignored(self, path: Path) -> bool:
        resolved = path.resolve(strict=False)
        parts = path.parts
        for root in self.roots.values():
            if resolved.is_relative_to(root):
                parts = resolved.relative_to(root).parts
                break
        return any(part in self.ignored_names for part in parts) or self._is_sensitive_path(path)

    def _reject_ignored(self, path: Path) -> None:
        if self._is_ignored(path):
            raise WorkspacePathError("Workspace path is excluded by the host read policy")

    @staticmethod
    def _is_sensitive_path(path: Path) -> bool:
        name = path.name.casefold()
        return (
            name == ".env"
            or name.startswith(".env.")
            or path.suffix.casefold() in {".key", ".p12", ".pem"}
        )


class WorkspaceReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=2_000)


class WorkspaceWriteInput(WorkspaceReadInput):
    content: str


def workspace_tools(filesystem: WorkspaceFilesystem) -> tuple[ToolDefinition, ToolDefinition]:
    async def read(context: AgentContext, arguments: BaseModel) -> dict[str, str]:
        if not isinstance(arguments, WorkspaceReadInput):
            raise TypeError("Workspace read arguments were not validated")
        _require_workspace(context, filesystem)
        return {"content": filesystem.read_text(arguments.root, arguments.path)}

    async def write(context: AgentContext, arguments: BaseModel) -> dict[str, int]:
        if not isinstance(arguments, WorkspaceWriteInput):
            raise TypeError("Workspace write arguments were not validated")
        _require_workspace(context, filesystem)
        size = filesystem.write_text(arguments.root, arguments.path, arguments.content)
        return {"bytes_written": size}

    return (
        ToolDefinition(
            name="workspace.read_text",
            description="Read a UTF-8 file inside an allowlisted workspace root.",
            input_model=WorkspaceReadInput,
            handler=read,
            permissions=frozenset({"workspace.read"}),
        ),
        ToolDefinition(
            name="workspace.write_text",
            description="Atomically write UTF-8 content inside an allowlisted workspace root.",
            input_model=WorkspaceWriteInput,
            handler=write,
            permissions=frozenset({"workspace.write"}),
            side_effect="reversible",
            requires_approval=True,
        ),
    )


def _require_workspace(context: AgentContext, filesystem: WorkspaceFilesystem) -> None:
    if context.metadata.get("workspace_id") != filesystem.workspace_id:
        raise WorkspacePathError("Agent context is not bound to this workspace")
    if filesystem.tenant_id is not None and context.tenant_id != filesystem.tenant_id:
        raise WorkspacePathError("Agent context tenant is not bound to this workspace")
    if filesystem.user_id is not None and context.user_id != filesystem.user_id:
        raise WorkspacePathError("Agent context user is not bound to this workspace")


def _search_matcher(query: str, *, regex: bool, case_sensitive: bool) -> Any:
    if not regex:
        expected = query if case_sensitive else query.casefold()

        def literal(line: str) -> bool:
            candidate = line if case_sensitive else line.casefold()
            return expected in candidate

        return literal
    _validate_safe_regex(query)
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled = re.compile(query, flags)
    return lambda line: compiled.search(line[:10_000]) is not None


def _validate_safe_regex(pattern: str) -> None:
    if len(pattern) > 200:
        raise ValueError("Regex search pattern exceeds the safe length limit")
    if "(?" in pattern or re.search(r"\\[1-9]", pattern):
        raise ValueError("Regex lookarounds, extensions, and backreferences are unsupported")
    if re.search(r"\([^)]*[+*{][^)]*\)[+*{]", pattern):
        raise ValueError("Nested quantified regex groups are unsupported")
    if re.search(r"(?:\*|\+|\?|\{[^}]+\})(?:\*|\+|\?|\{)", pattern):
        raise ValueError("Repeated regex quantifiers are unsupported")
