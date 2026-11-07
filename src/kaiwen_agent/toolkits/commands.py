from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kaiwen_agent.audit import SecretRedactor
from kaiwen_agent.context import AgentContext
from kaiwen_agent.integrations import WorkspaceFilesystem, WorkspacePathError
from kaiwen_agent.tools.definition import ToolDefinition


class SandboxUnavailableError(RuntimeError):
    pass


class CommandConfigurationError(ValueError):
    pass


class CommandParameter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    pattern: str = Field(
        default=r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,199}$",
        min_length=1,
        max_length=500,
    )
    choices: frozenset[str] = Field(default_factory=frozenset)

    def validate_value(self, value: str) -> str:
        if len(value) > 500 or re.fullmatch(self.pattern, value) is None:
            raise ValueError(f"Command parameter is invalid: {self.name}")
        if self.choices and value not in self.choices:
            raise ValueError(f"Command parameter is outside its choices: {self.name}")
        return value


class CommandPreset(BaseModel):
    """A host-authored exact argv template; it grants no execution permission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    description: str = Field(default="", max_length=2_000)
    category: str = Field(
        default="command",
        pattern=r"^(command|test|lint|typecheck|build)$",
    )
    executable: str = Field(min_length=1, max_length=500)
    arguments: tuple[str, ...] = ()
    parameters: tuple[CommandParameter, ...] = ()
    working_directory: str = Field(default=".", min_length=1, max_length=2_000)
    timeout_seconds: float = Field(default=60, gt=0, le=3_600)
    max_output_bytes: int = Field(default=250_000, ge=1, le=10_000_000)
    network_enabled: bool = False
    writable_workspace: bool = True

    @model_validator(mode="after")
    def validate_template(self) -> CommandPreset:
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("Command parameter names must be unique")
        if "\x00" in self.executable:
            raise ValueError("Command executable contains an invalid character")
        placeholders: set[str] = set()
        for argument in self.arguments:
            match = re.fullmatch(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}", argument)
            if match is not None:
                placeholders.add(match.group(1))
            elif "{" in argument or "}" in argument or "\x00" in argument:
                raise ValueError("Command arguments allow only whole-token placeholders")
        if placeholders != set(names):
            raise ValueError("Command placeholders must exactly match parameter declarations")
        return self


class SandboxCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str
    argv: tuple[str, ...] = Field(min_length=1)
    workspace: Path
    working_directory: Path
    timeout_seconds: float
    max_output_bytes: int
    network_enabled: bool = False
    writable_workspace: bool = True


class SandboxResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = Field(default=0, ge=0)
    timed_out: bool = False
    cancelled: bool = False
    artifact_refs: tuple[str, ...] = ()


class SandboxRunner(Protocol):
    async def run(self, command: SandboxCommand) -> SandboxResult: ...


class UnsupportedSandboxRunner:
    async def run(self, command: SandboxCommand) -> SandboxResult:
        del command
        raise SandboxUnavailableError(
            "No validated isolated process runner is configured for this host"
        )


class FakeSandboxRunner:
    """Deterministic test adapter; it never starts a process."""

    def __init__(
        self,
        results: dict[str, SandboxResult] | None = None,
        *,
        delay_seconds: float = 0,
    ) -> None:
        self.results = dict(results or {})
        self.delay_seconds = delay_seconds
        self.calls: list[SandboxCommand] = []

    async def run(self, command: SandboxCommand) -> SandboxResult:
        self.calls.append(command)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        try:
            return self.results[command.command_id]
        except KeyError as error:
            raise SandboxUnavailableError(
                f"No fake sandbox result configured for {command.command_id}"
            ) from error


class CommandRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, max_length=200)
    parameters: dict[str, str] = Field(default_factory=dict)


class CommandCatalog:
    def __init__(
        self,
        *,
        filesystem: WorkspaceFilesystem,
        root: str,
        runner: SandboxRunner,
        presets: Iterable[CommandPreset] = (),
        redactor: SecretRedactor | None = None,
    ) -> None:
        if root not in filesystem.roots:
            raise WorkspacePathError("Command root is not allowlisted by the workspace")
        self.filesystem = filesystem
        self.root = root
        self.runner = runner
        self.redactor = redactor or SecretRedactor()
        self._presets: dict[str, CommandPreset] = {}
        for preset in presets:
            self.register(preset)

    def register(self, preset: CommandPreset) -> None:
        existing = self._presets.get(preset.id)
        if existing is not None:
            if existing == preset:
                return
            raise CommandConfigurationError(f"Conflicting command preset: {preset.id}")
        self._presets[preset.id] = preset

    def get(self, command_id: str) -> CommandPreset:
        try:
            return self._presets[command_id]
        except KeyError as error:
            raise KeyError(f"Unknown command preset: {command_id}") from error

    async def execute(
        self,
        context: AgentContext,
        request: CommandRunInput,
        *,
        category: str | None = None,
    ) -> SandboxResult:
        self.filesystem.require_context(context)
        preset = self.get(request.command_id)
        if category is not None and preset.category != category:
            raise PermissionError(f"Command preset is not registered for {category}")
        declared = {parameter.name: parameter for parameter in preset.parameters}
        if set(request.parameters) != set(declared):
            raise ValueError("Command parameters must exactly match the preset")
        values = {
            name: parameter.validate_value(request.parameters[name])
            for name, parameter in declared.items()
        }
        argv = (preset.executable,) + tuple(
            values[argument[1:-1]]
            if argument.startswith("{") and argument.endswith("}")
            else argument
            for argument in preset.arguments
        )
        workspace = self.filesystem.roots[self.root]
        working_directory = (workspace / preset.working_directory).resolve(strict=True)
        if not working_directory.is_relative_to(workspace) or not working_directory.is_dir():
            raise WorkspacePathError("Command working directory escapes the workspace")
        result = await self.runner.run(
            SandboxCommand(
                command_id=preset.id,
                argv=argv,
                workspace=workspace,
                working_directory=working_directory,
                timeout_seconds=preset.timeout_seconds,
                max_output_bytes=preset.max_output_bytes,
                network_enabled=preset.network_enabled,
                writable_workspace=preset.writable_workspace,
            )
        )
        if len(result.stdout.encode()) + len(result.stderr.encode()) > preset.max_output_bytes:
            raise ValueError("Sandbox result exceeds the configured output limit")
        return result.model_copy(
            update={
                "stdout": self.redactor.redact(result.stdout),
                "stderr": self.redactor.redact(result.stderr),
            }
        )


def command_tool(catalog: CommandCatalog) -> ToolDefinition:
    async def run(context: AgentContext, arguments: BaseModel) -> object:
        if not isinstance(arguments, CommandRunInput):
            raise TypeError("Command arguments were not validated")
        return (await catalog.execute(context, arguments, category="command")).model_dump(
            mode="json"
        )

    return ToolDefinition(
        name="commands.run_allowed",
        description="Run one host-registered command preset in the configured isolated runner.",
        input_model=CommandRunInput,
        handler=run,
        permissions=frozenset({"commands.execute"}),
        side_effect="destructive",
        requires_approval=True,
        idempotent=False,
        timeout_seconds=3_700,
    )
