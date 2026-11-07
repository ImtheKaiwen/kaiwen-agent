"""First-party Docker `SandboxRunner`.

Untrusted project code runs only inside a container created from a trusted, host-selected
image. The runner builds the `docker` argument vector itself from constants and validated
configuration; model input never reaches an image name, a flag, a mount, or a host path.
It never falls back to host execution: any failure to verify the engine, the image, or the
isolation settings raises `SandboxUnavailableError`.

Containers reduce risk; they are not a guarantee against container escape or a malicious
or compromised Docker daemon. See `docs/coding-kits/PERMISSIONS_AND_SANDBOX.md`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kaiwen_agent.audit import SecretRedactor
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.toolkits.commands import SandboxCommand, SandboxResult, SandboxUnavailableError
from kaiwen_agent.toolkits.snapshot import (
    ExecutionSnapshot,
    SnapshotBuilder,
    SnapshotError,
    SnapshotLimits,
)

logger = logging.getLogger(__name__)

_DIGEST_REF = re.compile(r"^[a-z0-9][a-z0-9._/:-]{0,200}@sha256:[0-9a-f]{64}$")
_IMAGE_REF = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,300}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
_CLIENT_ENV = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "APPDATA",
    "LOCALAPPDATA",
    "TEMP",
    "TMP",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "XDG_RUNTIME_DIR",
)
_FLOOD_FACTOR = 64
_TIMEOUT_EXIT_CODE = 124
_CONTAINER_WORKSPACE = "/workspace"


class DockerSandboxConfig(BaseModel):
    """Trusted host configuration. Nothing here is model-controllable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str = Field(min_length=1, max_length=400)
    require_digest: bool = True
    docker_path: str | None = None
    user: str = Field(default="65534:65534", pattern=r"^[0-9]{1,9}:[0-9]{1,9}$")
    memory_mb: int = Field(default=512, ge=16, le=65_536)
    cpus: float = Field(default=1.0, gt=0, le=64)
    pids_limit: int = Field(default=128, ge=8, le=32_768)
    tmpfs_mb: int = Field(default=64, ge=1, le=4_096)
    allow_network: bool = False
    environment: Mapping[str, str] = Field(default_factory=lambda: {"HOME": "/tmp"})
    engine_timeout_seconds: float = Field(default=30, gt=0, le=600)
    cleanup_timeout_seconds: float = Field(default=15, gt=0, le=120)
    snapshot_limits: SnapshotLimits = Field(default_factory=SnapshotLimits)
    symlinks: str = Field(default="reject", pattern=r"^(reject|skip)$")

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        if _IMAGE_REF.fullmatch(value) is None or value.startswith(("-", "/")):
            raise ValueError("image must be a plain image reference")
        return value

    @field_validator("user")
    @classmethod
    def reject_root(cls, value: str) -> str:
        uid, gid = (int(part) for part in value.split(":"))
        if uid == 0 or gid == 0:
            raise ValueError("The sandbox must not run as root")
        return value

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        redactor = SecretRedactor()
        for name, item in value.items():
            if _ENV_NAME.fullmatch(name) is None or "\x00" in item or "\n" in item:
                raise ValueError("Sandbox environment names or values are invalid")
            if len(item) > 1_000 or redactor.redact(f"{name}={item}") != f"{name}={item}":
                raise ValueError("Sandbox environment must not contain secrets")
        return dict(value)

    def model_post_init(self, _context: object) -> None:
        if self.require_digest and _DIGEST_REF.fullmatch(self.image) is None:
            raise ValueError(
                "image must be pinned by digest (name@sha256:...); "
                "set require_digest=False only for local development"
            )


@dataclass(frozen=True, slots=True)
class DockerImageIdentity:
    reference: str
    image_id: str


class ProcessHandle(Protocol):
    @property
    def stdout(self) -> asyncio.StreamReader | None: ...

    @property
    def stderr(self) -> asyncio.StreamReader | None: ...

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    def kill(self) -> None: ...


ProcessLauncher = Callable[[Sequence[str], Mapping[str, str]], Awaitable[ProcessHandle]]


async def _launch(argv: Sequence[str], env: Mapping[str, str]) -> ProcessHandle:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
    )
    return process  # asyncio's Process satisfies ProcessHandle


class DockerSandboxRunner:
    """Runs one `SandboxCommand` in a locked-down container over an execution snapshot."""

    provides_isolation = True

    def __init__(
        self,
        config: DockerSandboxConfig,
        *,
        filesystem: WorkspaceFilesystem,
        root: str,
        launcher: ProcessLauncher | None = None,
        temporary_directory: Path | None = None,
        _snapshot: ExecutionSnapshot | None = None,
        _shared: _SharedState | None = None,
    ) -> None:
        self.config = config
        self.filesystem = filesystem
        self.root = root
        self._launcher = launcher or _launch
        self._temporary_directory = temporary_directory
        self._snapshot = _snapshot
        self._shared = _shared or _SharedState()
        self._redactor = SecretRedactor()
        if config.docker_path is not None:
            self._docker = config.docker_path
        else:
            self._docker = shutil.which("docker") or ""

    def bound_to(self, snapshot: ExecutionSnapshot) -> DockerSandboxRunner:
        """A runner that always executes against `snapshot` instead of a fresh capture.

        Evidence produced through the bound runner carries `snapshot.fingerprint`, so it
        is distinguishable from execution against the current workspace.
        """

        if snapshot.workspace_id != self.filesystem.workspace_id or snapshot.root != self.root:
            raise SandboxUnavailableError("Snapshot does not belong to this workspace root")
        return DockerSandboxRunner(
            self.config,
            filesystem=self.filesystem,
            root=self.root,
            launcher=self._launcher,
            temporary_directory=self._temporary_directory,
            _snapshot=snapshot,
            _shared=self._shared,
        )

    def capture(self) -> ExecutionSnapshot:
        """Capture the current workspace under the read policy (host-side, no execution)."""

        return SnapshotBuilder(
            self.filesystem,
            self.root,
            limits=self.config.snapshot_limits,
            symlinks="skip" if self.config.symlinks == "skip" else "reject",
        ).capture()

    async def verify(self) -> DockerImageIdentity:
        """Check the engine and the local image. Never pulls; fails closed."""

        if self._shared.identity is not None:
            return self._shared.identity
        async with self._shared.lock:
            if self._shared.identity is None:
                self._shared.identity = await self._verify_engine()
            return self._shared.identity

    def build_run_argv(
        self,
        *,
        container_name: str,
        mount_source: Path,
        command: SandboxCommand,
        container_workdir: str,
    ) -> tuple[str, ...]:
        """The complete `docker run` argument vector. Pure; exposed for auditing."""

        if command.network_enabled and not self.config.allow_network:
            raise SandboxUnavailableError("The command requires network access, which is disabled")
        source = str(mount_source)
        if any(character in source for character in (",", '"', "\n", "\x00")):
            raise SandboxUnavailableError("Sandbox work directory path is unsupported")
        memory = f"{self.config.memory_mb}m"
        mount = f"type=bind,source={source},target={_CONTAINER_WORKSPACE}"
        if not command.writable_workspace:
            mount += ",readonly"
        argv: list[str] = [
            self._docker,
            "run",
            "--pull=never",
            "--rm",
            "--name",
            container_name,
            "--label",
            "kaiwen.sandbox=1",
            "--network",
            "bridge" if command.network_enabled else "none",
            "--user",
            self.config.user,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            str(self.config.pids_limit),
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--cpus",
            f"{self.config.cpus:g}",
            "--ulimit",
            "core=0",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={self.config.tmpfs_mb}m",
            "--mount",
            mount,
            "--workdir",
            container_workdir,
            "--log-driver",
            "none",
            "--entrypoint",
            command.argv[0],
        ]
        for name in sorted(self.config.environment):
            argv.extend(["--env", f"{name}={self.config.environment[name]}"])
        argv.append(self.config.image)
        argv.extend(command.argv[1:])
        return tuple(argv)

    async def run(self, command: SandboxCommand) -> SandboxResult:
        identity = await self.verify()
        owned: ExecutionSnapshot | None = None
        snapshot = self._snapshot
        try:
            if snapshot is None:
                if command.workspace != self.filesystem.roots[self.root]:
                    raise SandboxUnavailableError("Command workspace is not the configured root")
                try:
                    owned = snapshot = await asyncio.to_thread(self.capture)
                except SnapshotError as error:
                    logger.warning("Workspace snapshot rejected: %s", error)
                    raise SandboxUnavailableError(
                        "The workspace cannot be snapshotted for sandbox execution"
                    ) from error
            return await self._execute(command, snapshot, identity)
        finally:
            if owned is not None:
                owned.close()

    async def _execute(
        self, command: SandboxCommand, snapshot: ExecutionSnapshot, identity: DockerImageIdentity
    ) -> SandboxResult:
        workdir = _container_workdir(command, self.filesystem.roots[self.root], snapshot)
        run_directory = Path(
            tempfile.mkdtemp(prefix="kaiwen-run-", dir=self._temporary_directory)
        ).resolve()
        name = f"kaiwen-sandbox-{uuid.uuid4().hex}"
        started = time.monotonic()
        try:
            if os.name == "posix":
                os.chmod(run_directory, 0o711)
            workspace_copy = run_directory / "workspace"
            try:
                await asyncio.to_thread(
                    snapshot.materialize, workspace_copy, writable=command.writable_workspace
                )
            except SnapshotError as error:
                logger.warning("Snapshot materialization failed: %s", error)
                raise SandboxUnavailableError("The execution snapshot is not intact") from error
            argv = self.build_run_argv(
                container_name=name,
                mount_source=workspace_copy,
                command=command,
                container_workdir=workdir,
            )
            return await self._run_container(
                argv,
                name=name,
                command=command,
                started=started,
                snapshot=snapshot,
                identity=identity,
            )
        finally:
            await asyncio.shield(self._remove_container(name))
            shutil.rmtree(run_directory, ignore_errors=True)

    async def _run_container(
        self,
        argv: tuple[str, ...],
        *,
        name: str,
        command: SandboxCommand,
        started: float,
        snapshot: ExecutionSnapshot,
        identity: DockerImageIdentity,
    ) -> SandboxResult:
        limit = max(1, command.max_output_bytes // 2)
        try:
            process = await self._launcher(argv, self._client_environment())
        except OSError as error:
            logger.warning("Docker client could not be started: %s", type(error).__name__)
            raise SandboxUnavailableError("The container engine could not be started") from error
        stdout = _BoundedReader(process.stdout, limit, limit * _FLOOD_FACTOR)
        stderr = _BoundedReader(process.stderr, limit, limit * _FLOOD_FACTOR)
        readers = asyncio.gather(stdout.read(), stderr.read())
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=command.timeout_seconds)
            await asyncio.wait_for(readers, timeout=self.config.cleanup_timeout_seconds)
        except TimeoutError:
            timed_out = True
        except asyncio.CancelledError:
            readers.cancel()
            await asyncio.shield(self._terminate(process, name))
            raise
        if timed_out or stdout.flooded or stderr.flooded:
            readers.cancel()
            await asyncio.shield(self._terminate(process, name))
            timed_out = timed_out or stdout.flooded or stderr.flooded
        returncode = _TIMEOUT_EXIT_CODE if timed_out else (process.returncode or 0)
        out, err = stdout.text(), stderr.text()
        if not timed_out and returncode == 125 and not out and err.startswith("docker:"):
            logger.warning("Container engine rejected the run request")
            raise SandboxUnavailableError("The container engine rejected the sandbox run")
        return SandboxResult(
            exit_code=returncode,
            stdout=out,
            stderr=err,
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            snapshot_fingerprint=snapshot.fingerprint,
            image_ref=identity.reference,
            image_id=identity.image_id,
            output_truncated=stdout.truncated or stderr.truncated,
        )

    async def _terminate(self, process: ProcessHandle, name: str) -> None:
        await self._remove_container(name)
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=self.config.cleanup_timeout_seconds)

    async def _remove_container(self, name: str) -> None:
        with contextlib.suppress(Exception):
            await self._engine(
                ["rm", "-f", "-v", name], timeout=self.config.cleanup_timeout_seconds
            )

    async def _verify_engine(self) -> DockerImageIdentity:
        if not self._docker:
            raise SandboxUnavailableError("The container engine is not configured")
        try:
            code, out = await self._engine(["version", "--format", "{{.Server.Os}}"])
        except (OSError, TimeoutError) as error:
            logger.warning("Docker engine check failed: %s", type(error).__name__)
            raise SandboxUnavailableError("The container engine is unavailable") from error
        if code != 0:
            raise SandboxUnavailableError("The container engine is unavailable")
        if out.strip() != "linux":
            raise SandboxUnavailableError("A linux container engine is required")
        try:
            code, out = await self._engine(
                [
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}} {{range .RepoDigests}}{{.}} {{end}}",
                    self.config.image,
                ]
            )
        except (OSError, TimeoutError) as error:
            raise SandboxUnavailableError("The container engine is unavailable") from error
        fields = out.split()
        if code != 0 or not fields or not re.fullmatch(r"sha256:[0-9a-f]{64}", fields[0]):
            raise SandboxUnavailableError("The sandbox image is not available locally")
        if "@sha256:" in self.config.image:
            pinned = self.config.image.split("@", 1)[1]
            repo_digests = {item.split("@", 1)[1] for item in fields[1:] if "@" in item}
            if pinned not in repo_digests:
                raise SandboxUnavailableError("The sandbox image does not match its pinned digest")
        return DockerImageIdentity(reference=self.config.image, image_id=fields[0])

    async def _engine(
        self, arguments: Sequence[str], *, timeout: float | None = None
    ) -> tuple[int, str]:
        process = await self._launcher([self._docker, *arguments], self._client_environment())
        limit = timeout or self.config.engine_timeout_seconds
        stdout = _BoundedReader(process.stdout, 64_000, 1_000_000)
        stderr = _BoundedReader(process.stderr, 64_000, 1_000_000)
        try:
            await asyncio.wait_for(
                asyncio.gather(stdout.read(), stderr.read(), process.wait()), timeout=limit
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            raise
        return (process.returncode or 0), stdout.text()

    @staticmethod
    def _client_environment() -> dict[str, str]:
        return {key: os.environ[key] for key in _CLIENT_ENV if key in os.environ}


class _SharedState:
    def __init__(self) -> None:
        self.identity: DockerImageIdentity | None = None
        self.lock = asyncio.Lock()


class _BoundedReader:
    """Keeps at most `limit` bytes, discards the rest, and flags runaway output."""

    def __init__(self, stream: asyncio.StreamReader | None, limit: int, flood: int) -> None:
        self._stream = stream
        self._limit = limit
        self._flood = flood
        self._buffer = bytearray()
        self._seen = 0
        self.truncated = False
        self.flooded = False

    async def read(self) -> None:
        if self._stream is None:
            return
        while True:
            chunk = await self._stream.read(65_536)
            if not chunk:
                return
            self._seen += len(chunk)
            room = self._limit - len(self._buffer)
            if room > 0:
                self._buffer.extend(chunk[:room])
            if self._seen > self._limit:
                self.truncated = True
            if self._seen > self._flood:
                self.flooded = True
                return

    def text(self) -> str:
        return bytes(self._buffer).decode("utf-8", errors="replace")


def _container_workdir(command: SandboxCommand, root: Path, snapshot: ExecutionSnapshot) -> str:
    try:
        relative = command.working_directory.resolve(strict=False).relative_to(root)
    except ValueError as error:
        raise SandboxUnavailableError("Working directory is outside the workspace") from error
    posix = PurePosixPath(relative.as_posix())
    if ".." in posix.parts:
        raise SandboxUnavailableError("Working directory is outside the workspace")
    if not snapshot.has_directory(posix.as_posix()):
        raise SandboxUnavailableError("Working directory is not part of the snapshot")
    return (
        str(PurePosixPath(_CONTAINER_WORKSPACE) / posix)
        if posix.parts != (".",)
        else (_CONTAINER_WORKSPACE)
    )
