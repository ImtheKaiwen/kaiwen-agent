"""Execution snapshot and fingerprint tests. These use the real filesystem, no containers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kaiwen_agent.integrations import WorkspaceFilesystem, WorkspacePathError
from kaiwen_agent.kits.coding import ProjectDiscovery
from kaiwen_agent.toolkits import SnapshotBuilder, SnapshotError, SnapshotLimits


def make_project(tmp_path: Path) -> tuple[Path, WorkspaceFilesystem]:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='demo'\n")
    (root / "src" / "demo.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests" / "test_demo.py").write_text("from src.demo import add\n")
    filesystem = WorkspaceFilesystem(workspace_id="workspace-1", roots={"main": root})
    return root, filesystem


def capture(filesystem: WorkspaceFilesystem, **options):  # type: ignore[no-untyped-def]
    return SnapshotBuilder(filesystem, "main", **options).capture()


def can_symlink(tmp_path: Path) -> bool:
    target = tmp_path / "probe-target"
    target.write_text("x")
    link = tmp_path / "probe-link"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        return False
    return True


def test_discovery_fingerprint_ignores_source_content_but_snapshot_does_not(
    tmp_path: Path,
) -> None:
    """Reproduces the M6.2 gap: ProjectDiscovery.fingerprint covers paths and manifests only."""

    root, filesystem = make_project(tmp_path)
    discovery = ProjectDiscovery(filesystem)
    before_discovery = discovery.discover("main").fingerprint
    with capture(filesystem) as before:
        (root / "src" / "demo.py").write_text("def add(a, b):\n    return a - b\n")
        assert discovery.discover("main").fingerprint == before_discovery
        with capture(filesystem) as after:
            assert after.fingerprint != before.fingerprint


def test_fingerprint_is_deterministic_and_covers_paths_contents_and_modes(tmp_path: Path) -> None:
    root, filesystem = make_project(tmp_path)
    with capture(filesystem) as first, capture(filesystem) as second:
        assert first.fingerprint == second.fingerprint
        assert [item.path for item in first.files] == [
            "pyproject.toml",
            "src/demo.py",
            "tests/test_demo.py",
        ]
    (root / "tests" / "test_demo.py").rename(root / "tests" / "test_other.py")
    with capture(filesystem) as renamed:
        assert renamed.fingerprint != first.fingerprint
    (root / "tests" / "test_other.py").rename(root / "tests" / "test_demo.py")
    (root / "pyproject.toml").write_text("[project]\nname='changed'\n")
    with capture(filesystem) as manifest:
        assert manifest.fingerprint != first.fingerprint


def test_protected_and_ignored_paths_are_excluded_and_do_not_change_fingerprint(
    tmp_path: Path,
) -> None:
    root, filesystem = make_project(tmp_path)
    with capture(filesystem) as baseline:
        (root / ".env").write_text("TOKEN=secret")
        (root / "server.pem").write_text("KEY")
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("[core]")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "dep.js").write_text("x")
        with capture(filesystem) as with_protected:
            assert with_protected.fingerprint == baseline.fingerprint
            names = {item.path for item in with_protected.files}
            assert not any(
                part in {".env", "server.pem", ".git", "node_modules"}
                for name in names
                for part in name.split("/")
            )
            copied = {
                path.relative_to(with_protected.directory).as_posix()
                for path in with_protected.directory.rglob("*")
                if path.is_file()
            }
            assert copied == names


def test_symlinks_are_rejected_by_default_and_skippable(tmp_path: Path) -> None:
    root, filesystem = make_project(tmp_path)
    if not can_symlink(tmp_path):
        pytest.skip("Symbolic links cannot be created on this host")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("do not copy")
    (root / "src" / "escape.txt").symlink_to(outside)
    with pytest.raises(SnapshotError):
        capture(filesystem)
    with capture(filesystem, symlinks="skip") as skipped:
        assert all("escape" not in item.path for item in skipped.files)
        assert not any(path.name == "escape.txt" for path in skipped.directory.rglob("*"))
        assert b"do not copy" not in b"".join(
            path.read_bytes() for path in skipped.directory.rglob("*") if path.is_file()
        )


def test_directory_symlink_escape_is_rejected(tmp_path: Path) -> None:
    root, filesystem = make_project(tmp_path)
    if not can_symlink(tmp_path):
        pytest.skip("Symbolic links cannot be created on this host")
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "data.txt").write_text("secret")
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Directory symbolic links cannot be created on this host")
    with pytest.raises(SnapshotError):
        capture(filesystem)


@pytest.mark.skipif(os.name != "posix", reason="FIFOs require POSIX")
def test_special_files_are_rejected(tmp_path: Path) -> None:
    root, filesystem = make_project(tmp_path)
    os.mkfifo(root / "pipe")
    with pytest.raises(SnapshotError):
        capture(filesystem)


def test_oversized_and_too_many_files_fail_closed_without_partial_snapshot(
    tmp_path: Path,
) -> None:
    root, filesystem = make_project(tmp_path)
    with pytest.raises(SnapshotError):
        capture(filesystem, limits=SnapshotLimits(max_files=2))
    with pytest.raises(SnapshotError):
        capture(filesystem, limits=SnapshotLimits(max_file_bytes=10))
    with pytest.raises(SnapshotError):
        capture(filesystem, limits=SnapshotLimits(max_total_bytes=20))
    temporary = tmp_path / "scratch"
    temporary.mkdir()
    with pytest.raises(SnapshotError):
        capture(filesystem, limits=SnapshotLimits(max_files=1), temporary_directory=temporary)
    assert list(temporary.iterdir()) == []


def test_unknown_root_and_host_files_are_never_modified(tmp_path: Path) -> None:
    root, filesystem = make_project(tmp_path)
    with pytest.raises(WorkspacePathError):
        SnapshotBuilder(filesystem, "missing")
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with capture(filesystem) as snapshot:
        target = tmp_path / "run"
        snapshot.materialize(target, writable=True)
        (target / "src" / "demo.py").write_text("tampered in the run copy")
        (target / "created-by-tests.txt").write_text("new")
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    assert not any(path.name.startswith("created-by") for path in root.rglob("*"))


def test_snapshot_tampering_is_detected_and_close_removes_files(tmp_path: Path) -> None:
    _, filesystem = make_project(tmp_path)
    snapshot = capture(filesystem)
    directory = snapshot.directory
    (directory / "src" / "demo.py").write_text("def add(a, b):\n    return 0 \n")
    with pytest.raises(SnapshotError):
        snapshot.materialize(tmp_path / "run", writable=False)
    snapshot.close()
    assert not directory.exists()
    with pytest.raises(SnapshotError):
        snapshot.materialize(tmp_path / "run2", writable=False)


def test_captured_bytes_are_those_that_were_hashed(tmp_path: Path) -> None:
    root, filesystem = make_project(tmp_path)
    with capture(filesystem) as snapshot:
        (root / "src" / "demo.py").write_text("changed after capture")
        target = tmp_path / "run"
        snapshot.materialize(target, writable=False)
        assert (target / "src" / "demo.py").read_text() == "def add(a, b):\n    return a + b\n"
