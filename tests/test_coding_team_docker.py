"""M6.4-B: the coding team with a model-driven Tester whose `testing.run` runs in Docker.

`examples/coding-team-acceptance/main.py --docker` composes the same team as M6.4-A, but the
Tester is an `AgentRuntimeWorker` whose (scripted) model calls the registered `testing.run` tool.
The command executes in `DockerSandboxRunner` (digest-pinned image, no network, read-only root), the
trusted pipeline records signed evidence, and the completion gate and team acceptance decide.

Two kinds of tests live here. The fail-closed tests need no engine and always run: they pin that a
missing sandbox runs nothing and never falls back to the local runner, and that the platform refuses
an unattended model-driven Tester on a runner that does not declare isolation. The container tests
use a real engine and are skipped without one (CI sets `KAIWEN_REQUIRE_DOCKER=1` on Linux, where an
unavailable engine is a failure). A skipped run proves nothing about container isolation.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kaiwen_agent.artifacts import ArtifactContext
from kaiwen_agent.toolkits import (
    CommandConfigurationError,
    DockerSandboxConfig,
    SandboxUnavailableError,
)
from kaiwen_agent.workflow import WorkflowOutcomeStatus
from tests.test_coding_team_runner import Spawned, run
from tests.test_docker_integration import _READY, _REASON
from tests.test_docker_integration import IMAGE as TEST_IMAGE

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "coding-team-acceptance" / "main.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("coding_team_acceptance_example_docker", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


example = load()
needs_docker = pytest.mark.skipif(not _READY, reason=f"Real Docker unavailable: {_REASON}")


def named(name: str) -> Any:
    return next(s for s in example.DOCKER_SCENARIOS if s.name == name)


def missing_engine(tmp_path: Path) -> DockerSandboxConfig:
    """A trusted configuration whose Docker client does not exist: the sandbox cannot start."""

    return DockerSandboxConfig(image=TEST_IMAGE, docker_path=str(tmp_path / "no-such-docker"))


# --------------------------------------------------------------------- fail closed (no engine)


def test_every_docker_scenario_is_model_driven_and_none_uses_the_host_step() -> None:
    assert example.DOCKER_SCENARIOS and all(s.tester == "model" for s in example.DOCKER_SCENARIOS)
    assert all(s.tester == "host" for s in example.SCENARIOS)  # the local ones stay host-run


def test_a_missing_sandbox_runs_nothing_and_never_falls_back_to_the_local_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the local runner must never be constructed for a model Tester")

    monkeypatch.setattr(example.LocalSnapshotRunner, "__init__", forbidden)
    spawned = Spawned(monkeypatch)
    scenario = named("docker_accepted")
    with pytest.raises(SandboxUnavailableError):
        run(example.run_scenario(scenario, tmp_path, docker_config=missing_engine(tmp_path)))
    assert spawned.processes == []  # no process of any kind was started


def test_the_command_line_reports_an_unavailable_sandbox_and_can_require_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = missing_engine(tmp_path)
    monkeypatch.delenv("KAIWEN_REQUIRE_DOCKER", raising=False)
    assert run(example.main_async(["docker_accepted"], docker=True, docker_config=config)) == 0
    assert "nothing was executed" in capsys.readouterr().out
    monkeypatch.setenv("KAIWEN_REQUIRE_DOCKER", "1")  # an unavailable sandbox is then a failure
    assert run(example.main_async(["docker_accepted"], docker=True, docker_config=config)) == 1
    assert "KAIWEN_REQUIRE_DOCKER is set" in capsys.readouterr().out
    assert run(example.main_async(["no-such-scenario"], docker=True)) == 2


def test_the_platform_refuses_an_unattended_model_tester_on_a_runner_without_isolation(
    tmp_path: Path,
) -> None:
    def local(filesystem: Any) -> Any:
        return example.LocalSnapshotRunner(example.SnapshotBuilder(filesystem, "main"))

    with pytest.raises(CommandConfigurationError):
        run(example.run_scenario(named("docker_accepted"), tmp_path, sandbox_factory=local))


# ------------------------------------------------------------------ real containers (Linux CI)


def evidence_payloads(result: Any) -> dict[str, dict[str, Any]]:
    """The run's recorded evidence artifacts by command id (read through the artifact manager)."""

    async def read() -> dict[str, dict[str, Any]]:
        context = ArtifactContext(
            tenant_id="local",
            user_id="local",
            session_id="session-1",
            trace_id="audit",
            run_id=result.run_id,
        )
        found: dict[str, dict[str, Any]] = {}
        for metadata in await result.artifacts.list(context, limit=50):
            artifact = await result.artifacts.read(context, metadata.id)
            payload = json.loads(artifact.content)
            payload["_label_fingerprint"] = metadata.labels.get("input_fingerprint")
            payload["_id"] = metadata.id
            found[str(metadata.labels.get("command_id"))] = payload
        return found

    return run(read())  # type: ignore[no-any-return]


@needs_docker
@pytest.mark.parametrize("scenario", example.DOCKER_SCENARIOS, ids=lambda s: s.name)
def test_each_docker_scenario_ends_as_documented(tmp_path: Path, scenario: Any) -> None:
    config = DockerSandboxConfig(image=TEST_IMAGE)
    result = run(example.run_scenario(scenario, tmp_path, docker_config=config))
    assert result.as_expected, (result.outcome.status, result.outcome.reason, result.acceptance)
    delivered = result.outcome.status is WorkflowOutcomeStatus.COMPLETED
    assert (result.run_status == "completed") is delivered  # only an accepted delivery completes


@needs_docker
def test_the_accepted_delivery_rests_on_signed_container_evidence(tmp_path: Path) -> None:
    config = DockerSandboxConfig(image=TEST_IMAGE)
    result = run(example.run_scenario(named("docker_accepted"), tmp_path, docker_config=config))
    assert result.outcome.status is WorkflowOutcomeStatus.COMPLETED
    decision = result.outcome.team_acceptance
    assert decision is not None and decision.accepted and len(decision.evidence_refs) == 1
    payloads = evidence_payloads(result)
    tests = payloads["unit.tests"]
    assert tests["_id"] == decision.evidence_refs[0]  # the review cited the gate-verified artifact
    assert tests["run_id"] == result.run_id and tests["workspace_id"] == "project"
    assert tests["exit_code"] == 0 and tests["origin"] == "trusted_sandbox"
    assert tests["image"]["reference"] == TEST_IMAGE  # the digest-pinned image, from the runner
    assert tests["_label_fingerprint"] == result.outcome.verified_input_fingerprint


@needs_docker
def test_a_failing_test_is_recorded_as_failed_and_the_models_claim_changes_nothing(
    tmp_path: Path,
) -> None:
    config = DockerSandboxConfig(image=TEST_IMAGE)
    result = run(
        example.run_scenario(named("docker_failing_tests"), tmp_path, docker_config=config)
    )
    assert result.outcome.status is WorkflowOutcomeStatus.BLOCKED
    assert result.outcome.reason == "quality_blocked" and result.acceptance == "not_evaluated"
    assert evidence_payloads(result)["unit.tests"]["exit_code"] != 0  # the container ran and failed
    tester = next(t for t in result.outcome.tasks if t.metadata["logical_task_id"] == "test")
    assert "All tests pass" in str((tester.output or {}).get("summary"))  # the claim existed
    assert (tester.output or {}).get("verified") is False  # and was never verification


@needs_docker
@pytest.mark.parametrize("name", ["docker_unregistered_command", "docker_unregistered_tool"])
def test_an_unregistered_tool_or_command_runs_nothing_and_records_no_evidence(
    tmp_path: Path, name: str
) -> None:
    config = DockerSandboxConfig(image=TEST_IMAGE)
    result = run(example.run_scenario(named(name), tmp_path, docker_config=config))
    assert result.outcome.status is WorkflowOutcomeStatus.FAILED
    assert result.run_status != "completed" and evidence_payloads(result) == {}
