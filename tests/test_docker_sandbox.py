"""Docker runner tests with a scripted engine.

These tests exercise the runner's argument construction, fail-closed behavior, limits,
timeout, cancellation and cleanup against a fake process launcher. They prove control
flow and the exact `docker` argument vector; they do not prove real container isolation.
That is covered by `tests/test_docker_integration.py`, which needs a real engine.
"""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.toolkits import (
    DockerSandboxConfig,
    DockerSandboxRunner,
    SandboxCommand,
    SandboxUnavailableError,
)

DIGEST = "sha256:" + "a" * 64
IMAGE = f"python@{DIGEST}"
IMAGE_ID = "sha256:" + "b" * 64
DOCKER = "/trusted/docker"


class FakeProcess:
    """Process stand-in; its stream readers are created lazily inside the running loop."""

    def __init__(
        self, *, stdout: bytes = b"", stderr: bytes = b"", code: int = 0, hang: bool = False
    ) -> None:
        self._data = (stdout, stderr)
        self._code = code
        self._hang = hang
        self._ready = False
        self.returncode: int | None = None
        self.killed = False

    def _ensure(self) -> None:
        if self._ready:
            return
        self._ready = True
        self._stdout = asyncio.StreamReader()
        self._stderr = asyncio.StreamReader()
        self._killed = asyncio.Event()
        self._stdout.feed_data(self._data[0])
        self._stderr.feed_data(self._data[1])
        if not self._hang:
            self._finish()

    @property
    def stdout(self) -> asyncio.StreamReader:
        self._ensure()
        return self._stdout

    @property
    def stderr(self) -> asyncio.StreamReader:
        self._ensure()
        return self._stderr

    def _finish(self) -> None:
        self._stdout.feed_eof()
        self._stderr.feed_eof()
        self.returncode = self._code

    async def wait(self) -> int:
        self._ensure()
        if self._hang:
            await self._killed.wait()
            self._finish()
        return self._code

    def kill(self) -> None:
        self._ensure()
        self.killed = True
        self._code = 137
        self._killed.set()


class FakeDocker:
    """Scripted engine. Records every launched argument vector."""

    def __init__(
        self,
        *,
        server_os: str = "linux",
        image_known: bool = True,
        image_digests: str | None = None,
        run: FakeProcess | None = None,
        on_run: Callable[[Path], None] | None = None,
    ) -> None:
        self.on_run = on_run
        self.mount_modes: list[int] = []
        self.server_os = server_os
        self.image_known = image_known
        self.image_digests = image_digests if image_digests is not None else f"python@{DIGEST}"
        self.run_process = run
        self.calls: list[tuple[str, ...]] = []
        self.environments: list[Mapping[str, str]] = []
        self.mounts: list[Path] = []
        self.mount_snapshots: list[dict[str, str]] = []
        self.processes: list[FakeProcess] = []

    async def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> FakeProcess:
        self.calls.append(tuple(argv))
        self.environments.append(dict(env))
        command = argv[1]
        if command == "version":
            process = FakeProcess(stdout=f"{self.server_os}\n".encode())
        elif command == "image":
            if self.image_known:
                process = FakeProcess(stdout=f"{IMAGE_ID} {self.image_digests}\n".encode())
            else:
                process = FakeProcess(stderr=b"Error: No such image", code=1)
        elif command == "run":
            for item in argv:
                if item.startswith("type=bind,source="):
                    source = Path(item.split("source=", 1)[1].split(",target=", 1)[0])
                    self.mounts.append(source)
                    self.mount_modes.append(stat.S_IMODE(source.parent.stat().st_mode))
                    if self.on_run is not None:
                        self.on_run(source)
                    self.mount_snapshots.append(
                        {
                            path.relative_to(source).as_posix(): path.read_text()
                            for path in source.rglob("*")
                            if path.is_file()
                        }
                    )
            process = self.run_process or FakeProcess(stdout=b"ok\n")
        else:
            process = FakeProcess()
        self.processes.append(process)
        return process

    def commands(self, name: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[1] == name]


def project(tmp_path: Path) -> tuple[Path, WorkspaceFilesystem]:
    root = tmp_path / "project"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("VALUE = 1\n")
    (root / "README.md").write_text("demo\n")
    return root, WorkspaceFilesystem(workspace_id="workspace-1", roots={"main": root})


def make_runner(
    tmp_path: Path, docker: FakeDocker, **config: object
) -> tuple[DockerSandboxRunner, Path, Path]:
    root, filesystem = project(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    runner = DockerSandboxRunner(
        DockerSandboxConfig(
            image=IMAGE,
            docker_path=DOCKER,
            **{"user": "65534:65534", **config},  # type: ignore[arg-type]
        ),
        filesystem=filesystem,
        root="main",
        launcher=docker,
        temporary_directory=scratch,
    )
    return runner, root, scratch


def command(
    root: Path,
    *,
    argv: tuple[str, ...] = ("python", "-m", "unittest"),
    timeout: float = 5,
    max_output: int = 10_000,
    network: bool = False,
    writable: bool = True,
    working_directory: str = ".",
) -> SandboxCommand:
    return SandboxCommand(
        command_id="unit",
        argv=argv,
        workspace=root.resolve(),
        working_directory=(root / working_directory).resolve(),
        timeout_seconds=timeout,
        max_output_bytes=max_output,
        network_enabled=network,
        writable_workspace=writable,
    )


def test_config_rejects_unsafe_settings() -> None:
    with pytest.raises(ValidationError):
        DockerSandboxConfig(image="python:3.12")  # not digest pinned
    DockerSandboxConfig(image="python:3.12", require_digest=False)
    with pytest.raises(ValidationError):
        DockerSandboxConfig(image=IMAGE, user="0:0")
    with pytest.raises(ValidationError):
        DockerSandboxConfig(image=IMAGE, user="1000:0")
    with pytest.raises(ValidationError):
        DockerSandboxConfig(image="--privileged", require_digest=False)
    with pytest.raises(ValidationError):
        DockerSandboxConfig(image=IMAGE, environment={"API_KEY": "sk-abcdefghijklmnopqrstuvwxyz"})
    with pytest.raises(ValidationError):
        DockerSandboxConfig(image=IMAGE, environment={"BAD NAME": "x"})


def test_run_argument_vector_is_locked_down(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner, root, _ = make_runner(tmp_path, docker)
    argv = runner.build_run_argv(
        container_name="kaiwen-sandbox-test",
        mount_source=tmp_path / "work",
        command=command(root),
        container_workdir="/workspace",
    )
    joined = " ".join(argv)
    assert argv[:3] == (DOCKER, "run", "--pull=never")
    assert "--network none" in joined
    assert "--user 65534:65534" in joined
    for required in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"):
        assert required in argv
    assert "--pids-limit 128" in joined
    assert "--memory 512m" in joined and "--memory-swap 512m" in joined
    assert "--cpus 1" in joined
    assert "--tmpfs /tmp:rw,nosuid,nodev,size=64m" in joined
    assert argv[argv.index("--entrypoint") + 1] == "python"
    assert argv[argv.index(IMAGE) + 1 :] == ("-m", "unittest")
    mount = argv[argv.index("--mount") + 1]
    assert mount == f"type=bind,source={tmp_path / 'work'},target=/workspace"
    for forbidden in ("--privileged", "--pid=host", "--ipc=host", "--userns=host", "docker.sock"):
        assert forbidden not in joined
    assert "-v" not in argv and "--volume" not in argv
    networks = [argv[i + 1] for i, item in enumerate(argv[:-1]) if item == "--network"]
    assert networks == ["none"]
    assert "--cap-add" not in joined and "--device" not in joined


def test_read_only_workspace_and_network_policy(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner, root, _ = make_runner(tmp_path, docker)
    argv = runner.build_run_argv(
        container_name="n",
        mount_source=tmp_path / "w",
        command=command(root, writable=False),
        container_workdir="/workspace",
    )
    assert argv[argv.index("--mount") + 1].endswith(",readonly")
    with pytest.raises(SandboxUnavailableError):
        runner.build_run_argv(
            container_name="n",
            mount_source=tmp_path / "w",
            command=command(root, network=True),
            container_workdir="/workspace",
        )
    allowed, root2, _ = make_runner(tmp_path / "second", docker, allow_network=True)
    argv = allowed.build_run_argv(
        container_name="n",
        mount_source=tmp_path / "w",
        command=command(root2, network=True),
        container_workdir="/workspace",
    )
    assert argv[argv.index("--network") + 1] == "bridge"


def test_mount_path_with_option_separator_is_refused(tmp_path: Path) -> None:
    runner, root, _ = make_runner(tmp_path, FakeDocker())
    with pytest.raises(SandboxUnavailableError):
        runner.build_run_argv(
            container_name="n",
            mount_source=tmp_path / "a,readonly=false",
            command=command(root),
            container_workdir="/workspace",
        )


def test_successful_run_uses_snapshot_copy_and_reports_identity(tmp_path: Path) -> None:
    docker = FakeDocker(run=FakeProcess(stdout=b"3 tests OK\n"))
    runner, root, scratch = make_runner(tmp_path, docker)
    result = asyncio.run(runner.run(command(root)))
    assert result.exit_code == 0 and result.stdout == "3 tests OK\n"
    assert result.snapshot_fingerprint and len(result.snapshot_fingerprint) == 64
    assert result.image_ref == IMAGE and result.image_id == IMAGE_ID
    # The container saw a private copy, never the host workspace.
    assert docker.mounts and docker.mounts[0] != root.resolve()
    assert docker.mount_snapshots[0] == {"README.md": "demo\n", "pkg/mod.py": "VALUE = 1\n"}
    assert list(scratch.iterdir()) == []  # run directory and snapshot are cleaned up
    assert docker.commands("rm"), "the container is always removed"


def test_host_process_is_never_started_for_the_command(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner, root, _ = make_runner(tmp_path, docker)
    asyncio.run(runner.run(command(root, argv=("python", "-c", "print(1)"))))
    assert {call[0] for call in docker.calls} == {DOCKER}
    assert all("python" not in call[:2] for call in docker.calls)


def test_client_environment_is_an_allowlist_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abcdefghijklmnopqrstuvwxyz")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    docker = FakeDocker()
    runner, root, _ = make_runner(tmp_path, docker)
    asyncio.run(runner.run(command(root)))
    assert all("OPENAI_API_KEY" not in env for env in docker.environments)
    assert docker.environments[0].get("DOCKER_HOST") == "unix:///var/run/docker.sock"
    run_argv = docker.commands("run")[0]
    assert not any("OPENAI" in item or "sk-" in item for item in run_argv)


@pytest.mark.parametrize(
    "docker",
    [
        FakeDocker(server_os="windows"),
        FakeDocker(image_known=False),
        FakeDocker(image_digests="python@sha256:" + "c" * 64),
        FakeDocker(image_digests=""),
    ],
    ids=["not-linux", "image-missing", "digest-mismatch", "no-repo-digest"],
)
def test_unavailable_engine_or_image_fails_closed_without_running(
    tmp_path: Path, docker: FakeDocker
) -> None:
    runner, root, scratch = make_runner(tmp_path, docker)
    with pytest.raises(SandboxUnavailableError) as raised:
        asyncio.run(runner.run(command(root)))
    assert not docker.commands("run")
    assert list(scratch.iterdir()) == []
    assert str(raised.value) and "sha256" not in str(raised.value)


def test_missing_docker_executable_never_falls_back_to_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    root, filesystem = project(tmp_path)
    launched: list[object] = []

    async def launcher(argv: Sequence[str], env: Mapping[str, str]) -> FakeProcess:
        launched.append(argv)
        raise AssertionError("nothing may be launched without an engine")

    runner = DockerSandboxRunner(
        DockerSandboxConfig(image=IMAGE), filesystem=filesystem, root="main", launcher=launcher
    )
    with pytest.raises(SandboxUnavailableError):
        asyncio.run(runner.run(command(root)))
    assert launched == []


def test_engine_that_cannot_start_is_reported_without_details(tmp_path: Path) -> None:
    root, filesystem = project(tmp_path)

    async def launcher(argv: Sequence[str], env: Mapping[str, str]) -> FakeProcess:
        raise FileNotFoundError("/secret/path/docker")

    runner = DockerSandboxRunner(
        DockerSandboxConfig(image=IMAGE, docker_path=DOCKER),
        filesystem=filesystem,
        root="main",
        launcher=launcher,
    )
    with pytest.raises(SandboxUnavailableError) as raised:
        asyncio.run(runner.run(command(root)))
    assert "secret" not in str(raised.value)


def test_engine_rejection_is_generic_and_hides_engine_text(tmp_path: Path) -> None:
    docker = FakeDocker(
        run=FakeProcess(stderr=b"docker: Error response from daemon: token=abc123", code=125)
    )
    runner, root, _ = make_runner(tmp_path, docker)
    with pytest.raises(SandboxUnavailableError) as raised:
        asyncio.run(runner.run(command(root)))
    assert "abc123" not in str(raised.value)


def test_command_failure_is_a_result_not_an_error(tmp_path: Path) -> None:
    docker = FakeDocker(run=FakeProcess(stdout=b"FAILED\n", stderr=b"boom\n", code=1))
    runner, root, _ = make_runner(tmp_path, docker)
    result = asyncio.run(runner.run(command(root)))
    assert (result.exit_code, result.timed_out) == (1, False)
    assert result.stderr == "boom\n"


def test_output_is_bounded_and_flagged(tmp_path: Path) -> None:
    docker = FakeDocker(run=FakeProcess(stdout=b"x" * 5_000_000, stderr=b"e" * 3_000))
    runner, root, _ = make_runner(tmp_path, docker)
    result = asyncio.run(runner.run(command(root, max_output=2_000, timeout=5)))
    assert len(result.stdout.encode()) <= 1_000
    assert len(result.stderr.encode()) <= 1_000
    assert result.output_truncated is True
    assert docker.commands("rm")


def test_timeout_kills_the_container_and_cleans_up(tmp_path: Path) -> None:
    docker = FakeDocker(run=FakeProcess(stdout=b"partial", hang=True))
    runner, root, scratch = make_runner(tmp_path, docker)
    result = asyncio.run(runner.run(command(root, timeout=0.2)))
    assert result.timed_out is True and result.exit_code == 124
    assert result.stdout == "partial"
    removed = docker.commands("rm")
    assert removed and removed[0][2:4] == ("-f", "-v")
    assert docker.processes[-1].killed or any(p.killed for p in docker.processes)
    assert list(scratch.iterdir()) == []


def test_cancellation_removes_container_and_temporary_files(tmp_path: Path) -> None:
    docker = FakeDocker(run=FakeProcess(hang=True))
    runner, root, scratch = make_runner(tmp_path, docker)

    async def scenario() -> None:
        task = asyncio.create_task(runner.run(command(root, timeout=30)))
        for _ in range(100):
            if docker.commands("run"):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert docker.commands("rm")
    assert any(process.killed for process in docker.processes)
    assert list(scratch.iterdir()) == []


def test_bound_runner_executes_the_captured_snapshot_not_the_current_workspace(
    tmp_path: Path,
) -> None:
    docker = FakeDocker()
    runner, root, scratch = make_runner(tmp_path, docker)
    snapshot = runner.capture()
    (root / "pkg" / "mod.py").write_text("VALUE = 2  # changed after capture\n")
    bound = runner.bound_to(snapshot)
    result = asyncio.run(bound.run(command(root)))
    assert result.snapshot_fingerprint == snapshot.fingerprint
    assert docker.mount_snapshots[0]["pkg/mod.py"] == "VALUE = 1\n"
    fresh = asyncio.run(runner.run(command(root)))
    assert fresh.snapshot_fingerprint != snapshot.fingerprint
    snapshot.close()
    assert [path for path in scratch.iterdir()] == []


def test_workspace_outside_the_configured_root_is_refused(tmp_path: Path) -> None:
    runner, root, _ = make_runner(tmp_path, FakeDocker())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    bad = command(root).model_copy(update={"workspace": elsewhere.resolve()})
    with pytest.raises(SandboxUnavailableError):
        asyncio.run(runner.run(bad))


def test_working_directory_must_be_inside_the_snapshot(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner, root, _ = make_runner(tmp_path, docker)
    (root / "pkg").mkdir(exist_ok=True)
    asyncio.run(runner.run(command(root, working_directory="pkg")))
    assert docker.commands("run")[0][docker.commands("run")[0].index("--workdir") + 1] == (
        "/workspace/pkg"
    )
    with pytest.raises(SandboxUnavailableError):
        asyncio.run(runner.run(command(root, working_directory="missing")))
    escaping = command(root).model_copy(update={"working_directory": tmp_path.resolve()})
    with pytest.raises(SandboxUnavailableError):
        asyncio.run(runner.run(escaping))


def test_workspace_with_symlink_or_secret_is_not_executed_unsafely(tmp_path: Path) -> None:
    docker = FakeDocker()
    runner, root, _ = make_runner(tmp_path, docker)
    (root / ".env").write_text("TOKEN=secret-value")
    asyncio.run(runner.run(command(root)))
    assert all(".env" not in snapshot for snapshot in docker.mount_snapshots)
    assert all("secret-value" not in text for s in docker.mount_snapshots for text in s.values())


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_run_directory_is_unreachable_by_other_local_users(tmp_path: Path) -> None:
    """The per-run copy is world-writable inside, so its parent must be owner-only."""

    docker = FakeDocker()
    runner, root, _ = make_runner(tmp_path, docker)
    asyncio.run(runner.run(command(root)))
    assert docker.mount_modes and all(mode & 0o077 == 0 for mode in docker.mount_modes)


def test_unmodified_snapshot_is_reported_after_a_normal_run(tmp_path: Path) -> None:
    def add_output(mount: Path) -> None:
        (mount / "new-output.txt").write_text("test artifact")  # new files are legitimate

    runner, root, _ = make_runner(tmp_path, FakeDocker(on_run=add_output))
    result = asyncio.run(runner.run(command(root)))
    assert result.snapshot_unchanged is True and result.modified_paths == ()


@pytest.mark.parametrize("mutation", ["edit", "delete", "touch-content"])
def test_modifying_snapshot_files_during_a_command_is_detected(
    tmp_path: Path, mutation: str
) -> None:
    def mutate(mount: Path) -> None:
        target = mount / "pkg" / "mod.py"
        if mutation == "delete":
            target.unlink()
        elif mutation == "edit":
            target.write_text("VALUE = 2\n")
        else:
            target.write_text("VALUE = 1\n ")  # same prefix, different bytes and size

    runner, root, _ = make_runner(tmp_path, FakeDocker(on_run=mutate))
    result = asyncio.run(runner.run(command(root)))
    assert result.exit_code == 0
    assert result.snapshot_unchanged is False
    assert result.modified_paths == ("pkg/mod.py",)
    assert (root / "pkg" / "mod.py").read_text() == "VALUE = 1\n"


def test_replacing_a_snapshot_file_with_a_symlink_is_detected(tmp_path: Path) -> None:
    def swap(mount: Path) -> None:
        target = mount / "pkg" / "mod.py"
        target.unlink()
        try:
            target.symlink_to(mount / "README.md")
        except (OSError, NotImplementedError):
            pytest.skip("Symbolic links cannot be created on this host")

    runner, root, _ = make_runner(tmp_path, FakeDocker(on_run=swap))
    result = asyncio.run(runner.run(command(root)))
    assert result.snapshot_unchanged is False


def test_modification_is_reported_after_a_timeout_too(tmp_path: Path) -> None:
    docker = FakeDocker(
        run=FakeProcess(hang=True), on_run=lambda m: (m / "README.md").write_text("x")
    )
    runner, root, _ = make_runner(tmp_path, docker)
    result = asyncio.run(runner.run(command(root, timeout=0.2)))
    assert result.timed_out and result.snapshot_unchanged is False


def test_invalid_utf8_output_stays_within_the_encoded_byte_budget(tmp_path: Path) -> None:
    docker = FakeDocker(run=FakeProcess(stdout=b"\xff" * 1_000, stderr=b"\xfe" * 1_000))
    runner, root, _ = make_runner(tmp_path, docker)
    result = asyncio.run(runner.run(command(root, max_output=1_000)))
    assert len(result.stdout.encode()) <= 500
    assert len(result.stderr.encode()) <= 500
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= 1_000
    assert result.output_truncated is True


def test_multibyte_output_cut_inside_a_character_stays_within_budget(tmp_path: Path) -> None:
    docker = FakeDocker(run=FakeProcess(stdout="\u20ac".encode() * 300))  # 900 bytes
    runner, root, _ = make_runner(tmp_path, docker)
    result = asyncio.run(runner.run(command(root, max_output=1_000)))
    assert len(result.stdout.encode()) <= 500
    assert result.output_truncated is True
    assert set(result.stdout) <= {"\u20ac", "\ufffd"}


def test_output_exactly_at_the_budget_is_not_reported_truncated(tmp_path: Path) -> None:
    docker = FakeDocker(run=FakeProcess(stdout=b"a" * 500, stderr="\u00e9".encode() * 250))
    runner, root, _ = make_runner(tmp_path, docker)
    result = asyncio.run(runner.run(command(root, max_output=1_000)))
    assert result.stdout == "a" * 500 and result.stderr == "\u00e9" * 250
    assert result.output_truncated is False


def test_invalid_utf8_does_not_lose_the_result_or_evidence_in_the_catalog(
    tmp_path: Path,
) -> None:
    from kaiwen_agent.context import AgentContext
    from kaiwen_agent.toolkits import CommandCatalog, CommandPreset, CommandRunInput

    docker = FakeDocker(run=FakeProcess(stdout=b"\xff" * 2_000, code=1))
    runner, root, _ = make_runner(tmp_path, docker)
    catalog = CommandCatalog(
        filesystem=runner.filesystem,
        root="main",
        runner=runner,
        presets=(
            CommandPreset(
                id="unit",
                category="test",
                executable="python",
                arguments=("-m", "unittest"),
                max_output_bytes=1_000,
                timeout_seconds=5,
            ),
        ),
    )
    context = AgentContext(
        session_id="s", metadata={"workspace_id": "workspace-1"}, permissions=frozenset()
    )
    result = asyncio.run(catalog.execute(context, CommandRunInput(command_id="unit")))
    assert result.exit_code == 1 and result.output_truncated is True
    assert len(result.stdout.encode()) <= 500


def test_cleanup_failure_is_reported_not_treated_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import kaiwen_agent.toolkits.docker_sandbox as module

    runner, root, _ = make_runner(tmp_path, FakeDocker())
    assert asyncio.run(runner.run(command(root))).cleanup_failed is False
    monkeypatch.setattr(module, "_remove_tree", lambda _path: False)
    with caplog.at_level("ERROR", logger="kaiwen_agent.toolkits.docker_sandbox"):
        result = asyncio.run(runner.run(command(root)))
    assert result.cleanup_failed is True
    assert any("cleanup" in record.getMessage().lower() for record in caplog.records)


@pytest.mark.skipif(
    os.name != "posix" or getattr(os, "geteuid", lambda: 0)() == 0, reason="needs non-root POSIX"
)
def test_remove_tree_deletes_restrictive_directories_owned_by_the_host_user(
    tmp_path: Path,
) -> None:
    import kaiwen_agent.toolkits.docker_sandbox as module

    target = tmp_path / "run"
    (target / "out" / "deep").mkdir(parents=True)
    (target / "out" / "deep" / "file.txt").write_text("x")
    (target / "out" / "deep").chmod(0o500)
    (target / "out").chmod(0o500)
    assert module._remove_tree(target) is True
    assert not target.exists()
    assert module._remove_tree(target) is True  # already gone


@pytest.mark.skipif(os.name != "posix", reason="host user ids")
def test_default_container_user_is_the_non_root_host_user() -> None:
    config = DockerSandboxConfig(image=IMAGE)
    uid = os.getuid()  # type: ignore[attr-defined]
    expected = f"{uid}:{os.getgid()}" if uid != 0 else "65534:65534"  # type: ignore[attr-defined]
    assert config.effective_user == expected
    assert not config.effective_user.startswith("0:")
    assert DockerSandboxConfig(image=IMAGE, user="1234:4321").effective_user == "1234:4321"


def test_default_container_user_is_never_root_on_any_platform() -> None:
    uid, gid = DockerSandboxConfig(image=IMAGE).effective_user.split(":")
    assert int(uid) != 0 and int(gid) != 0
