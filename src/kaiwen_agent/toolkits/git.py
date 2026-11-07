from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.context import AgentContext
from kaiwen_agent.integrations import WorkspaceFilesystem, WorkspacePathError
from kaiwen_agent.integrations.filesystem import (
    SENSITIVE_FILE_NAMES,
    SENSITIVE_FILE_PREFIXES,
    SENSITIVE_FILE_SUFFIXES,
)
from kaiwen_agent.tools.definition import ToolDefinition

_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}~^:+-]{0,199}$")

# Git output is filtered by pathspec so that model-facing tools neither print the
# contents of protected files nor enumerate their paths. The filter is derived from the
# same effective policy as WorkspaceFilesystem, including host-configured ignored names.
# Configuration applied to every Git process so repository configuration cannot start
# external programs: no filesystem monitor, no hooks, no signature verification program.
# The hooks path is the null device, so no hook file can exist beneath it.
_BASE_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "log.showSignature=false",
)
_FILTER_KEY = re.compile(
    r"filter\.(?P<subsection>.+)\.(?:clean|smudge|process|required)", re.IGNORECASE
)
_DIFF_DRIVER_KEY = re.compile(r"diff\.(?P<subsection>.+)\.(?:textconv|command)", re.IGNORECASE)
_PROGRAM_KEY = re.compile(r"diff\.external|gpg\.(?:.+\.)?program", re.IGNORECASE)

_GLOB_CLASSES = {"[": "[[]", "*": "[*]", "?": "[?]"}


def _glob_literal(name: str) -> str:
    """Escape glob metacharacters with bracket classes, which Git for Windows keeps intact.

    Backslash escapes are rewritten to path separators on Windows, so names that cannot
    be expressed safely are rejected and the Git tools fail closed.
    """

    if not name or "\\" in name or "/" in name or "\x00" in name or "\n" in name:
        raise ValueError("Ignored name cannot be expressed as a safe Git pathspec")
    return "".join(_GLOB_CLASSES.get(character, character) for character in name)


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

    def exclude_pathspecs(self) -> tuple[str, ...]:
        """Exclusion pathspecs for the effective workspace read policy."""

        specs = [
            *(f":(exclude,glob,icase)**/{_glob_literal(name)}" for name in SENSITIVE_FILE_NAMES),
            *(
                f":(exclude,glob,icase)**/{_glob_literal(prefix)}*"
                for prefix in SENSITIVE_FILE_PREFIXES
            ),
            *(
                f":(exclude,glob,icase)**/*{_glob_literal(suffix)}"
                for suffix in SENSITIVE_FILE_SUFFIXES
            ),
        ]
        for name in self.filesystem.ignored_names:
            literal = _glob_literal(name)
            specs.append(f":(exclude,glob,icase)**/{literal}")
            specs.append(f":(exclude,glob,icase)**/{literal}/**")
        return tuple(sorted(set(specs)))

    def validate_revision(self, revision: str) -> str:
        # "rev:path" would read a blob by path and bypass the workspace read policy.
        if not _REVISION.fullmatch(revision) or revision.startswith("-") or ":" in revision:
            raise ValueError("Git revision is not allowed")
        return revision

    def validate_path(self, value: str) -> str:
        supplied = Path(value)
        if supplied.is_absolute() or "\x00" in value:
            raise WorkspacePathError("Git path must be relative")
        candidate = (self.repo_root / supplied).resolve(strict=False)
        if not candidate.is_relative_to(self.repo_root):
            raise WorkspacePathError("Git path escapes the repository")
        relative = candidate.relative_to(self.repo_root).as_posix()
        if relative != "." and self.filesystem.is_protected(self.root, relative):
            raise WorkspacePathError("Git path is excluded by the host read policy")
        return relative

    async def run(self, *arguments: str) -> dict[str, object]:
        overrides = await self._program_overrides()
        return await self._execute(*overrides, *arguments)

    async def _program_overrides(self) -> list[str]:
        """Neutralize repository-configured programs that read commands could start.

        A hostile repository can name programs in its own configuration (clean and
        smudge filters, text converters, external diff commands) and select them through
        attributes. Command-line configuration outranks repository configuration, so each
        such key found in the effective configuration is overridden before the real
        command runs. Keys that cannot be passed safely make the tool fail closed.
        """

        listing = await self._execute("config", "--list", "--name-only", "-z")
        overrides: dict[str, str] = {}
        for name in str(listing["stdout"]).split("\0"):
            if not name:
                continue
            filter_match = _FILTER_KEY.fullmatch(name)
            diff_match = _DIFF_DRIVER_KEY.fullmatch(name)
            if filter_match is not None:
                subsection = filter_match.group("subsection")
                for variable in ("clean", "smudge", "process"):
                    overrides[f"filter.{subsection}.{variable}"] = ""
                overrides[f"filter.{subsection}.required"] = "false"
            elif diff_match is not None:
                subsection = diff_match.group("subsection")
                overrides[f"diff.{subsection}.textconv"] = ""
                overrides[f"diff.{subsection}.command"] = ""
            elif _PROGRAM_KEY.fullmatch(name) is not None:
                overrides[name] = ""
            else:
                continue
            if "=" in name or "\n" in name:
                raise ValueError("Repository Git configuration cannot be neutralized safely")
        arguments: list[str] = []
        for key, value in sorted(overrides.items()):
            arguments.extend(("-c", f"{key}={value}"))
        return arguments

    async def _execute(self, *arguments: str) -> dict[str, object]:
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
            "--no-pager",
            *_BASE_CONFIG,
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
            "--ignore-submodules=all",
            "--",
            *repository.exclude_pathspecs(),
        )

    async def diff(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, GitDiffInput):
            raise TypeError("Git diff arguments were not validated")
        repository.require_context(context)
        argv = ["diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "--no-color"]
        if arguments.staged:
            argv.append("--cached")
        if arguments.revision is not None:
            argv.append(repository.validate_revision(arguments.revision))
        argv.append("--")
        if arguments.path is not None:
            argv.append(repository.validate_path(arguments.path))
        argv.extend(repository.exclude_pathspecs())
        return await repository.run(*argv)

    async def log(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, GitLogInput):
            raise TypeError("Git log arguments were not validated")
        repository.require_context(context)
        return await repository.run(
            "log",
            "--no-show-signature",
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
            "--no-textconv",
            "--ignore-submodules=all",
            "--no-show-signature",
            "--no-color",
            "--format=fuller",
            repository.validate_revision(arguments.revision),
            "--",
        ]
        if arguments.path is not None:
            argv.append(repository.validate_path(arguments.path))
        argv.extend(repository.exclude_pathspecs())
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
