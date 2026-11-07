"""Validated, content-addressed execution snapshots of a host-authorized workspace.

A snapshot is a private copy of the files that the workspace read policy allows, made
once from open file descriptors. The fingerprint is computed from the exact bytes that
were copied, so the bytes that are later executed are the bytes that were hashed.
Nothing here writes to the source workspace.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.integrations import WorkspaceFilesystem, WorkspacePathError

FINGERPRINT_VERSION = "kaiwen.snapshot.v1"
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)


class SnapshotError(ValueError):
    """The workspace cannot be captured safely; callers must not execute against it."""


class SnapshotLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_files: int = Field(default=5_000, ge=1, le=100_000)
    max_total_bytes: int = Field(default=64 * 1024 * 1024, ge=1, le=4 * 1024**3)
    max_file_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=1024**3)
    max_depth: int = Field(default=32, ge=1, le=256)


class SnapshotFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    size: int = Field(ge=0)
    sha256: str
    executable: bool = False


class ExecutionSnapshot:
    """An immutable private copy of a workspace plus its content fingerprint.

    The directory is only ever read; each execution runs against a fresh materialized
    copy (`materialize`), after re-verifying every file against its recorded digest.
    """

    def __init__(
        self,
        *,
        directory: Path,
        files: tuple[SnapshotFile, ...],
        fingerprint: str,
        workspace_id: str,
        root: str,
        total_bytes: int,
    ) -> None:
        self._directory = directory
        self.files = files
        self.fingerprint = fingerprint
        self.workspace_id = workspace_id
        self.root = root
        self.total_bytes = total_bytes
        self._closed = False

    @property
    def directory(self) -> Path:
        self._require_open()
        return self._directory

    @property
    def closed(self) -> bool:
        return self._closed

    def has_directory(self, relative: str) -> bool:
        parts = PurePosixPath(relative).parts
        if not parts or parts == (".",):
            return True
        prefix = "/".join(parts) + "/"
        return any(item.path.startswith(prefix) for item in self.files)

    def materialize(self, destination: Path, *, writable: bool) -> None:
        """Copy the verified snapshot into an empty `destination` directory.

        Every file is re-read and compared with its recorded digest, so tampering with
        the private snapshot directory is detected instead of executed.
        """

        self._require_open()
        if destination.exists() and any(destination.iterdir()):
            raise SnapshotError("Materialization target must be empty")
        destination.mkdir(parents=True, exist_ok=True)
        _chmod(destination, 0o777 if writable else 0o755)
        for item in self.files:
            source = self._directory / PurePosixPath(item.path)
            target = destination / PurePosixPath(item.path)
            _make_directories(destination, target.parent, writable=writable)
            data = _read_regular_file(source, item.size)
            if sha256(data).hexdigest() != item.sha256:
                raise SnapshotError("Snapshot content changed after capture")
            with open(target, "xb") as stream:
                stream.write(data)
            mode = 0o777 if item.executable else 0o666
            if not writable:
                mode &= 0o755 if item.executable else 0o644
            _chmod(target, mode)

    def changed_paths(self, directory: Path, *, limit: int = 20) -> tuple[str, ...]:
        """Snapshot files that are missing or differ in `directory` (new files are ignored).

        A file replaced by a symbolic link or a special file counts as changed. This is a
        post-hoc comparison against the recorded digests, not a guarantee that the bytes
        stayed unchanged for the whole run.
        """

        self._require_open()
        changed: list[str] = []
        for item in self.files:
            try:
                data = _read_regular_file(directory / PurePosixPath(item.path), item.size)
            except SnapshotError:
                data = None
            if data is None or sha256(data).hexdigest() != item.sha256:
                changed.append(item.path)
                if len(changed) >= limit:
                    break
        return tuple(changed)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self._directory, ignore_errors=True)

    def __enter__(self) -> ExecutionSnapshot:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise SnapshotError("Execution snapshot is closed")


class SnapshotBuilder:
    """Captures one allowlisted workspace root under the existing read/write policy.

    Protected and ignored paths (`.git`, secrets, dependency and build directories) are
    never copied. Symbolic links, special files, oversized or too many files, and paths
    that cannot be represented safely fail the capture; a partial snapshot is never
    returned. Excluded directories do not contribute to the fingerprint.
    """

    def __init__(
        self,
        filesystem: WorkspaceFilesystem,
        root: str,
        *,
        limits: SnapshotLimits | None = None,
        symlinks: Literal["reject", "skip"] = "reject",
        temporary_directory: Path | None = None,
    ) -> None:
        if root not in filesystem.roots:
            raise WorkspacePathError("Snapshot root is not allowlisted by the workspace")
        if symlinks not in {"reject", "skip"}:
            raise ValueError("symlinks must be 'reject' or 'skip'")
        self.filesystem = filesystem
        self.root = root
        self.limits = limits or SnapshotLimits()
        self.symlinks = symlinks
        self.temporary_directory = temporary_directory

    def capture(self) -> ExecutionSnapshot:
        base = self.filesystem.roots[self.root]
        directory = Path(
            tempfile.mkdtemp(prefix="kaiwen-snapshot-", dir=self.temporary_directory)
        ).resolve()
        _chmod(directory, 0o700)
        try:
            files, skipped, total = self._copy_tree(base, directory)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        fingerprint = _fingerprint(files, skipped)
        return ExecutionSnapshot(
            directory=directory,
            files=files,
            fingerprint=fingerprint,
            workspace_id=self.filesystem.workspace_id,
            root=self.root,
            total_bytes=total,
        )

    def _copy_tree(
        self, base: Path, directory: Path
    ) -> tuple[tuple[SnapshotFile, ...], tuple[str, ...], int]:
        files: list[SnapshotFile] = []
        skipped: list[str] = []
        total = 0
        stack: list[tuple[Path, PurePosixPath, int]] = [(base, PurePosixPath("."), 0)]
        while stack:
            current, relative_dir, depth = stack.pop()
            if depth > self.limits.max_depth:
                raise SnapshotError("Workspace is nested deeper than the snapshot depth limit")
            try:
                children = sorted(os.scandir(current), key=lambda entry: entry.name)
            except OSError as error:
                raise SnapshotError("Workspace directory cannot be read") from error
            for entry in children:
                relative = (
                    relative_dir / entry.name
                    if relative_dir.parts != (".",)
                    else (PurePosixPath(entry.name))
                )
                _validate_name(entry.name)
                if self._is_excluded(relative):
                    continue
                is_junction = getattr(entry, "is_junction", None)
                if entry.is_symlink() or (callable(is_junction) and is_junction()):
                    if self.symlinks == "reject":
                        raise SnapshotError("Workspace contains a symbolic link")
                    skipped.append(relative.as_posix())
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), relative, depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise SnapshotError("Workspace contains a special file")
                if len(files) >= self.limits.max_files:
                    raise SnapshotError("Workspace exceeds the snapshot file limit")
                data, executable = _read_for_capture(Path(entry.path), self.limits.max_file_bytes)
                total += len(data)
                if total > self.limits.max_total_bytes:
                    raise SnapshotError("Workspace exceeds the snapshot size limit")
                target = directory / PurePosixPath(relative.as_posix())
                _make_directories(directory, target.parent, writable=False, private=True)
                with open(target, "xb") as stream:
                    stream.write(data)
                files.append(
                    SnapshotFile(
                        path=relative.as_posix(),
                        size=len(data),
                        sha256=sha256(data).hexdigest(),
                        executable=executable,
                    )
                )
        files.sort(key=lambda item: item.path)
        return tuple(files), tuple(sorted(skipped)), total

    def _is_excluded(self, relative: PurePosixPath) -> bool:
        try:
            return self.filesystem.is_protected(self.root, relative.as_posix())
        except WorkspacePathError:
            # A path that cannot be resolved inside the root (for example a link that
            # escapes it) is not excluded by name; link handling decides what happens.
            return False


def _fingerprint(files: tuple[SnapshotFile, ...], skipped: tuple[str, ...]) -> str:
    material = {
        "version": FINGERPRINT_VERSION,
        "files": [[item.path, item.size, item.sha256, item.executable] for item in files],
        "skipped_links": list(skipped),
    }
    return sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validate_name(name: str) -> None:
    if not name or "\x00" in name or name in {".", ".."} or "/" in name or "\\" in name:
        raise SnapshotError("Workspace contains an unsafe file name")
    if any(ord(character) < 32 for character in name):
        raise SnapshotError("Workspace contains an unsafe file name")


def _read_for_capture(path: Path, max_bytes: int) -> tuple[bytes, bool]:
    flags = os.O_RDONLY | _O_NOFOLLOW | _O_BINARY | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SnapshotError("Workspace file cannot be opened safely") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotError("Workspace contains a special file")
        if info.st_size > max_bytes:
            raise SnapshotError("Workspace file exceeds the snapshot file size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(data) > max_bytes:
        raise SnapshotError("Workspace file exceeds the snapshot file size limit")
    return data, bool(info.st_mode & stat.S_IXUSR)


def _read_regular_file(path: Path, expected_size: int) -> bytes:
    flags = os.O_RDONLY | _O_NOFOLLOW | _O_BINARY | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SnapshotError("Snapshot file cannot be read") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SnapshotError("Snapshot file is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(expected_size + 1)
    finally:
        os.close(descriptor)
    if len(data) != expected_size:
        raise SnapshotError("Snapshot content changed after capture")
    return data


def _make_directories(
    base: Path, directory: Path, *, writable: bool, private: bool = False
) -> None:
    missing: list[Path] = []
    current = directory
    while current != base and not current.exists():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        item.mkdir()
        _chmod(item, 0o700 if private else 0o777 if writable else 0o755)


def _chmod(path: Path, mode: int) -> None:
    if os.name == "posix":
        os.chmod(path, mode)
