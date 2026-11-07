from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.context import AgentContext
from kaiwen_agent.integrations import WorkspaceFilesystem, WorkspacePathError
from kaiwen_agent.tools.definition import ToolDefinition

_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}~^:+-]{0,199}$")


class GitUnavailableError(RuntimeError):
    pass


class GitCommandError(RuntimeError):
    pass


class _GitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GitStatusInput(_GitInput):
    include_untracked: bool = True


class GitDiffInput(_GitInput):
    staged: bool = False
    revision: str | None = Field(default=None, min_length=1, max_length=200)
    path: str | None = Field(default=None, min_length=1, max_length=2_000)


class GitLogInput(_GitInput):
    max_count: int = Field(default=20, ge=1, le=100)


class GitShowInput(_GitInput):
    revision: str = Field(min_length=1, max_length=200)
    path: str | None = Field(default=None, min_length=1, max_length=2_000)


class ReadOnlyGitRepository:
    """Fixed, shell-free Git reads for one host-authorized workspace root."""

    def __init__(
        self,
        *,
        filesystem: WorkspaceFilesystem,
        root: str,
        timeout_seconds: float = 10,
        max_output_bytes: int = 250_000,
        executable: str | None = None,
    ) -> None:
        if root not in filesystem.roots:
            raise WorkspacePathError("Git root is not allowlisted by the workspace")
        repo_root = filesystem.roots[root]
        if not (repo_root / ".git").exists():
            raise ValueError("Git workspace root is not a repository")
        if timeout_seconds <= 0 or max_output_bytes < 1:
            raise ValueError("Git execution limits must be positive")
        selected = executable or shutil.which("git")
        if selected is None:
            raise GitUnavailableError("Git executable is unavailable")
        self.filesystem = filesystem
        self.root = root
        self.repo_root = repo_root
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.executable = selected

    def require_context(self, context: AgentContext) -> None:
        self.filesystem.require_context(context)

    def validate_revision(self, revision: str) -> str:
        if not _REVISION.fullmatch(revision) or revision.startswith("-"):
            raise ValueError("Git revision is not allowed")
        return revision

    def validate_path(self, value: str) -> str:
        supplied = Path(value)
        if supplied.is_absolute() or "\x00" in value:
            raise WorkspacePathError("Git path must be relative")
        candidate = (self.repo_root / supplied).resolve(strict=False)
        if not candidate.is_relative_to(self.repo_root):
            raise WorkspacePathError("Git path escapes the repository")
        return candidate.relative_to(self.repo_root).as_posix()

    async def run(self, *arguments: str) -> dict[str, object]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
        }
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C",
            }
        )
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "-C",
            str(self.repo_root),
            "--no-optional-locks",
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        assert process.stdout is not None and process.stderr is not None
        try:
            stdout, stderr, return_code = await asyncio.wait_for(
                asyncio.gather(
                    _read_bounded(process.stdout, self.max_output_bytes),
                    _read_bounded(process.stderr, self.max_output_bytes),
                    process.wait(),
                ),
                timeout=self.timeout_seconds,
            )
        except (TimeoutError, _OutputLimitError) as error:
            process.kill()
            await process.wait()
            if isinstance(error, TimeoutError):
                raise TimeoutError("Git read exceeded the configured timeout") from error
            raise ValueError("Git output exceeds the configured byte limit") from error
        decoded_out = stdout.decode("utf-8", errors="replace")
        decoded_error = stderr.decode("utf-8", errors="replace")
        if return_code != 0:
            raise GitCommandError(
                f"Git read failed with exit code {return_code}: {decoded_error[:1_000]}"
            )
        return {
            "exit_code": return_code,
            "stdout": decoded_out,
            "stderr": decoded_error,
        }


def git_read_tools(repository: ReadOnlyGitRepository) -> tuple[ToolDefinition, ...]:
    async def status(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, GitStatusInput):
            raise TypeError("Git status arguments were not validated")
        repository.require_context(context)
        untracked = "normal" if arguments.include_untracked else "no"
        return await repository.run(
            "status",
            "--short",
            "--branch",
            f"--untracked-files={untracked}",
        )

    async def diff(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, GitDiffInput):
            raise TypeError("Git diff arguments were not validated")
        repository.require_context(context)
        argv = ["diff", "--no-ext-diff", "--no-color"]
        if arguments.staged:
            argv.append("--cached")
        if arguments.revision is not None:
            argv.append(repository.validate_revision(arguments.revision))
        argv.append("--")
        if arguments.path is not None:
            argv.append(repository.validate_path(arguments.path))
        return await repository.run(*argv)

    async def log(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, GitLogInput):
            raise TypeError("Git log arguments were not validated")
        repository.require_context(context)
        return await repository.run(
            "log",
            "--no-color",
            f"--max-count={arguments.max_count}",
            "--format=%H%x09%aI%x09%an%x09%s",
        )

    async def show(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, GitShowInput):
            raise TypeError("Git show arguments were not validated")
        repository.require_context(context)
        argv = [
            "show",
            "--no-ext-diff",
            "--no-color",
            "--format=fuller",
            repository.validate_revision(arguments.revision),
            "--",
        ]
        if arguments.path is not None:
            argv.append(repository.validate_path(arguments.path))
        return await repository.run(*argv)

    permission = frozenset({"git.read"})
    return (
        ToolDefinition(
            name="git.status",
            description="Read bounded status for the host-authorized local Git repository.",
            input_model=GitStatusInput,
            handler=status,
            permissions=permission,
        ),
        ToolDefinition(
            name="git.diff",
            description="Read a bounded local Git diff without external diff helpers.",
            input_model=GitDiffInput,
            handler=diff,
            permissions=permission,
        ),
        ToolDefinition(
            name="git.log",
            description="Read a bounded local Git commit log.",
            input_model=GitLogInput,
            handler=log,
            permissions=permission,
        ),
        ToolDefinition(
            name="git.show",
            description="Read a bounded local Git revision or path change.",
            input_model=GitShowInput,
            handler=show,
            permissions=permission,
        ),
    )


class _OutputLimitError(RuntimeError):
    pass


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(8_192)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise _OutputLimitError
        chunks.append(chunk)
