"""Evidence recording, signed verification and approval policy for sandbox commands.

The runner output here is scripted (`ScriptedRunner`); these tests prove the evidence and
gate logic. A real container run feeding the same path is in `test_docker_integration.py`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kaiwen_agent.agents import VersionedReference
from kaiwen_agent.artifacts import ArtifactContext, ArtifactCreate, ArtifactManager, ArtifactType
from kaiwen_agent.context import AgentContext
from kaiwen_agent.exceptions import ApprovalRequiredError
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.kits.coding.standards import coding_standards_packs
from kaiwen_agent.kits.coding.verification import (
    CommandEvidenceRecorder,
    EvidenceBinding,
    EvidenceSigner,
    install_coding_verifiers,
)
from kaiwen_agent.quality import (
    CompletionGate,
    CompletionGateStatus,
    VerificationContext,
    VerifierRegistry,
)
from kaiwen_agent.standards import StandardsRegistry, StandardsResolver
from kaiwen_agent.toolkits import (
    CommandCatalog,
    CommandConfigurationError,
    CommandPreset,
    CommandRunInput,
    DockerSandboxConfig,
    DockerSandboxRunner,
    FakeSandboxRunner,
    SandboxCommand,
    SandboxResult,
    command_tool,
)
from kaiwen_agent.toolkits import testing_tools as build_testing_tools
from kaiwen_agent.tools import ToolExecutor
from kaiwen_agent.types import ToolCall

FINGERPRINT = "f" * 64
SIGNER = EvidenceSigner(key=b"0123456789abcdef-test-key")


class ScriptedRunner:
    """Returns a scripted result that carries snapshot identity. Not a container."""

    def __init__(self, result: SandboxResult) -> None:
        self.result = result
        self.calls: list[SandboxCommand] = []

    async def run(self, command: SandboxCommand) -> SandboxResult:
        self.calls.append(command)
        return self.result


def result(
    *,
    exit_code: int = 0,
    timed_out: bool = False,
    cancelled: bool = False,
    fingerprint: str | None = FINGERPRINT,
    stdout: str = "OK",
) -> SandboxResult:
    return SandboxResult(
        exit_code=exit_code,
        stdout=stdout,
        timed_out=timed_out,
        cancelled=cancelled,
        duration_seconds=0.5,
        snapshot_fingerprint=fingerprint,
        image_ref="python@sha256:" + "a" * 64,
        image_id="sha256:" + "b" * 64,
    )


def agent_context(
    *, run_id: str | None = "run-1", user: str = "user-1", tenant: str = "tenant-1"
) -> AgentContext:
    metadata: dict[str, object] = {"workspace_id": "workspace-1"}
    if run_id is not None:
        metadata["runtime_run_id"] = run_id
    return AgentContext(
        session_id="session-1",
        tenant_id=tenant,
        user_id=user,
        trace_id="trace-1",
        permissions=frozenset({"tests.run", "commands.execute"}),
        metadata=metadata,
    )


PRESET = CommandPreset(
    id="unit.tests",
    category="test",
    executable="python",
    arguments=("-m", "unittest"),
)


class Harness:
    def __init__(self, tmp_path: Path, runner: object, *, signer: EvidenceSigner | None = SIGNER):
        self.artifacts = ArtifactManager()
        self.recorder = CommandEvidenceRecorder(
            artifacts=self.artifacts,
            signer=SIGNER,
            bindings=[
                EvidenceBinding(
                    command_id="unit.tests",
                    requirement_id="coding.tests.run",
                    evidence_type="test_run",
                )
            ],
        )
        root = tmp_path / "project"
        root.mkdir(exist_ok=True)
        self.catalog = CommandCatalog(
            filesystem=WorkspaceFilesystem(workspace_id="workspace-1", roots={"main": root}),
            root="main",
            runner=runner,  # type: ignore[arg-type]
            presets=(PRESET,),
            observers=(self.recorder,),
        )
        registry = VerifierRegistry()
        install_coding_verifiers(registry, self.artifacts, signer=signer)
        self.gate = CompletionGate(verifiers=registry, artifacts=self.artifacts)
        self.resolution = StandardsResolver(StandardsRegistry(coding_standards_packs())).resolve(
            [VersionedReference(id="coding.tests", version=1)],
            condition_facts={"has_executable_tests": True},
        )

    async def execute(self, context: AgentContext | None = None) -> SandboxResult:
        return await self.catalog.execute(
            context or agent_context(), CommandRunInput(command_id="unit.tests"), category="test"
        )

    async def evaluate(
        self,
        *,
        run_id: str = "run-1",
        user: str = "user-1",
        tenant: str = "tenant-1",
        fingerprint: str = FINGERPRINT,
    ):  # type: ignore[no-untyped-def]
        return await self.gate.evaluate(
            self.resolution,
            context=VerificationContext(
                tenant_id=tenant,
                user_id=user,
                session_id="session-1",
                trace_id="trace-1",
                run_id=run_id,
                protected_input_fingerprint=fingerprint,
            ),
        )


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_passing_real_result_produces_pass_with_attribution(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ScriptedRunner(result()))

    async def scenario() -> None:
        executed = await harness.execute()
        assert len(executed.artifact_refs) == 1
        gate = await harness.evaluate()
        assert gate.status == CompletionGateStatus.PASS
        context = ArtifactContext(
            tenant_id="tenant-1", user_id="user-1", session_id="s", trace_id="t", run_id="run-1"
        )
        artifact = await harness.artifacts.read(context, executed.artifact_refs[0])
        payload = json.loads(artifact.content)
        assert payload["run_id"] == "run-1" and payload["workspace_id"] == "workspace-1"
        assert payload["requirement_id"] == "coding.tests.run"
        assert payload["command_id"] == "unit.tests" and payload["parameters"] == {}
        assert payload["snapshot_fingerprint"] == FINGERPRINT
        assert payload["image"]["id"] == "sha256:" + "b" * 64
        assert payload["exit_code"] == 0 and payload["outcome"] == "passed"
        assert payload["stdout"]["sha256"] and payload["stdout"]["bytes"] == 2
        labels = artifact.metadata.labels
        assert labels["input_fingerprint"] == FINGERPRINT and labels["evidence_origin"]

    run(scenario())


@pytest.mark.parametrize(
    ("scripted", "finding"),
    [
        (result(exit_code=1), "command_failed"),
        (result(exit_code=124, timed_out=True), "command_timed_out"),
        (result(exit_code=0, cancelled=True), "command_cancelled"),
        (result(exit_code=0, timed_out=True), "command_timed_out"),
    ],
    ids=["failed", "timeout", "cancelled", "timeout-with-zero-exit"],
)
def test_unsuccessful_executions_never_pass(
    tmp_path: Path, scripted: SandboxResult, finding: str
) -> None:
    harness = Harness(tmp_path, ScriptedRunner(scripted))

    async def scenario() -> None:
        await harness.execute()
        gate = await harness.evaluate()
        assert gate.status == CompletionGateStatus.BLOCKED
        assert finding in gate.verifications[0].findings

    run(scenario())


def test_missing_evidence_and_stale_fingerprint_do_not_pass(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ScriptedRunner(result()))

    async def scenario() -> None:
        assert (await harness.evaluate()).status == CompletionGateStatus.BLOCKED
        await harness.execute()
        assert (await harness.evaluate()).status == CompletionGateStatus.PASS
        # The project changed: the gate now expects another snapshot fingerprint.
        stale = await harness.evaluate(fingerprint="e" * 64)
        assert stale.status == CompletionGateStatus.BLOCKED

    run(scenario())


def test_later_passing_run_supersedes_earlier_failure(tmp_path: Path) -> None:
    runner = ScriptedRunner(result(exit_code=1))
    harness = Harness(tmp_path, runner)

    async def scenario() -> None:
        await harness.execute()
        assert (await harness.evaluate()).status == CompletionGateStatus.BLOCKED
        runner.result = result()
        await asyncio.sleep(0.01)
        await harness.execute()
        assert (await harness.evaluate()).status == CompletionGateStatus.PASS

    run(scenario())


def test_results_without_snapshot_identity_or_run_record_no_evidence(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ScriptedRunner(result(fingerprint=None)))

    async def scenario() -> None:
        executed = await harness.execute()
        assert executed.artifact_refs == ()
        assert (await harness.evaluate()).status == CompletionGateStatus.BLOCKED
        harness.catalog.runner = ScriptedRunner(result())  # type: ignore[assignment]
        assert (await harness.execute(agent_context(run_id=None))).artifact_refs == ()
        assert (await harness.evaluate()).status == CompletionGateStatus.BLOCKED

    run(scenario())


def test_fake_runner_cannot_produce_evidence(tmp_path: Path) -> None:
    fake = FakeSandboxRunner({"unit.tests": SandboxResult(exit_code=0, stdout="fake pass")})
    harness = Harness(tmp_path, fake)

    async def scenario() -> None:
        executed = await harness.execute()
        assert executed.artifact_refs == ()
        assert (await harness.evaluate()).status == CompletionGateStatus.BLOCKED

    run(scenario())


def test_model_authored_or_unsigned_evidence_is_rejected_by_trusted_verifier(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, ScriptedRunner(result(exit_code=1)))
    owner = ArtifactContext(
        tenant_id="tenant-1", user_id="user-1", session_id="s", trace_id="t", run_id="run-1"
    )

    async def scenario() -> None:
        forged_payload = json.dumps(
            {"status": "pass", "exit_code": 0, "summary": "all green"}
        ).encode()
        labels = {
            "requirement_id": "coding.tests.run",
            "evidence_type": "test_run",
            "input_fingerprint": FINGERPRINT,
        }
        await harness.artifacts.create(
            owner,
            ArtifactCreate(
                type=ArtifactType.REPORT,
                mime_type="application/json",
                content=forged_payload,
                labels=labels,
            ),
        )
        # Also forge every label, including the origin and a made-up MAC.
        await harness.artifacts.create(
            owner,
            ArtifactCreate(
                type=ArtifactType.REPORT,
                mime_type="application/json",
                content=forged_payload,
                labels={**labels, "evidence_origin": "trusted_sandbox", "evidence_mac": "0" * 64},
            ),
        )
        # A correctly shaped payload signed with a different key is rejected too.
        other = EvidenceSigner(key=b"another-host-key-0123456789")
        payload = {
            "origin": "trusted_sandbox",
            "run_id": "run-1",
            "requirement_id": "coding.tests.run",
            "snapshot_fingerprint": FINGERPRINT,
            "exit_code": 0,
            "timed_out": False,
            "cancelled": False,
        }
        content = json.dumps(payload).encode()
        signed = {**labels, "evidence_origin": "trusted_sandbox"}
        signed["evidence_mac"] = other.sign(
            tenant_id="tenant-1",
            owner_user_id="user-1",
            run_id="run-1",
            content=content,
            labels=signed,
        )
        await harness.artifacts.create(
            owner,
            ArtifactCreate(
                type=ArtifactType.REPORT,
                mime_type="application/json",
                content=content,
                labels=signed,
            ),
        )
        gate = await harness.evaluate()
        assert gate.status == CompletionGateStatus.BLOCKED
        assert "evidence_not_trusted" in gate.verifications[0].findings

    run(scenario())


def test_evidence_is_isolated_by_owner_and_run(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ScriptedRunner(result()))

    async def scenario() -> None:
        await harness.execute(agent_context(run_id="run-1", user="user-1"))
        assert (await harness.evaluate(run_id="run-2")).status == CompletionGateStatus.BLOCKED
        assert (await harness.evaluate(user="user-2")).status == CompletionGateStatus.BLOCKED
        assert (await harness.evaluate(tenant="tenant-2")).status == CompletionGateStatus.BLOCKED
        assert (await harness.evaluate()).status == CompletionGateStatus.PASS

    run(scenario())


def test_unbound_commands_record_nothing_and_recording_failures_do_not_pass(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, ScriptedRunner(result()))
    other = CommandPreset(id="other", category="test", executable="python", arguments=("-V",))
    harness.catalog.register(other)

    class Broken:
        async def observe(self, execution: object) -> tuple[str, ...]:
            raise RuntimeError("store down")

    async def scenario() -> None:
        unbound = await harness.catalog.execute(
            agent_context(), CommandRunInput(command_id="other"), category="test"
        )
        assert unbound.artifact_refs == ()
        harness.catalog.observers = (Broken(),)  # type: ignore[assignment]
        executed = await harness.execute()
        assert executed.exit_code == 0 and executed.artifact_refs == ()
        assert (await harness.evaluate()).status == CompletionGateStatus.BLOCKED

    run(scenario())


def test_recorder_redacts_output_and_bounds_text(tmp_path: Path) -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    harness = Harness(tmp_path, ScriptedRunner(result(stdout=f"token {secret} " + "x" * 50_000)))

    async def scenario() -> None:
        executed = await harness.execute()
        context = ArtifactContext(
            tenant_id="tenant-1", user_id="user-1", session_id="s", trace_id="t", run_id="run-1"
        )
        artifact = await harness.artifacts.read(context, executed.artifact_refs[0])
        assert secret.encode() not in artifact.content
        payload = json.loads(artifact.content)
        assert payload["stdout"]["text_truncated"] is True
        assert len(payload["stdout"]["text"]) <= 20_000

    run(scenario())


def isolated_runner(tmp_path: Path) -> DockerSandboxRunner:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    return DockerSandboxRunner(
        DockerSandboxConfig(image="python@sha256:" + "a" * 64, docker_path="/x/docker"),
        filesystem=WorkspaceFilesystem(workspace_id="workspace-1", roots={"main": root}),
        root="main",
    )


def catalog_for(tmp_path: Path, runner: object) -> CommandCatalog:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    return CommandCatalog(
        filesystem=WorkspaceFilesystem(workspace_id="workspace-1", roots={"main": root}),
        root="main",
        runner=runner,  # type: ignore[arg-type]
        presets=(PRESET,),
    )


def test_unattended_execution_requires_an_isolating_runner_and_safe_categories(
    tmp_path: Path,
) -> None:
    with pytest.raises(CommandConfigurationError):
        build_testing_tools(
            catalog_for(tmp_path, FakeSandboxRunner()),
            unattended_categories=frozenset({"test"}),
        )
    isolated = catalog_for(tmp_path, isolated_runner(tmp_path))
    with pytest.raises(CommandConfigurationError):
        build_testing_tools(isolated, unattended_categories=frozenset({"build"}))
    with pytest.raises(CommandConfigurationError):
        build_testing_tools(isolated, unattended_categories=frozenset({"command"}))
    tools = {
        tool.name: tool
        for tool in build_testing_tools(
            isolated, unattended_categories=frozenset({"test", "lint", "typecheck"})
        )
    }
    assert not tools["testing.run"].requires_approval
    assert not tools["quality.lint"].requires_approval
    assert not tools["quality.typecheck"].requires_approval
    assert tools["build.run"].requires_approval
    assert command_tool(isolated).requires_approval
    default = {tool.name: tool for tool in build_testing_tools(isolated)}
    assert all(tool.requires_approval for tool in default.values())


def test_unattended_test_runs_through_tool_executor_but_commands_still_need_approval(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, ScriptedRunner(result()))
    harness.catalog.runner.provides_isolation = True  # type: ignore[attr-defined]
    tools = {
        tool.name: tool
        for tool in build_testing_tools(harness.catalog, unattended_categories=frozenset({"test"}))
    }
    general = command_tool(harness.catalog)

    async def scenario() -> None:
        executed = await ToolExecutor().execute(
            tools["testing.run"],
            ToolCall(
                id="t1",
                name="testing.run",
                arguments={"command_id": "unit.tests", "parameters": {}},
            ),
            agent_context(),
            execution_scope="run-1",
        )
        assert executed.output["exit_code"] == 0 and executed.output["artifact_refs"]
        with pytest.raises(ApprovalRequiredError):
            await ToolExecutor().execute(
                general,
                ToolCall(
                    id="t2",
                    name="commands.run_allowed",
                    arguments={"command_id": "unit.tests", "parameters": {}},
                ),
                agent_context(),
                execution_scope="run-1",
            )

    run(scenario())


def test_invalid_presets_and_parameters_are_rejected(tmp_path: Path) -> None:
    catalog = catalog_for(tmp_path, ScriptedRunner(result()))
    with pytest.raises(ValueError):
        CommandPreset(id="bad", executable="python", arguments=("{unknown}",))
    with pytest.raises(ValueError):
        CommandPreset(id="bad2", executable="python", arguments=("-c", "x{y}"))

    async def scenario() -> None:
        with pytest.raises(KeyError):
            await catalog.execute(
                agent_context(), CommandRunInput(command_id="missing"), category="test"
            )
        with pytest.raises(ValueError):
            await catalog.execute(
                agent_context(),
                CommandRunInput(command_id="unit.tests", parameters={"extra": "x"}),
                category="test",
            )
        with pytest.raises(PermissionError):
            await catalog.execute(
                agent_context(), CommandRunInput(command_id="unit.tests"), category="lint"
            )

    run(scenario())


def test_legacy_unsigned_verifier_keeps_accepting_host_written_json(tmp_path: Path) -> None:
    artifacts = ArtifactManager()
    registry = VerifierRegistry()
    install_coding_verifiers(registry, artifacts)
    gate = CompletionGate(verifiers=registry, artifacts=artifacts)
    resolution = StandardsResolver(StandardsRegistry(coding_standards_packs())).resolve(
        [VersionedReference(id="coding.tests", version=1)],
        condition_facts={"has_executable_tests": True},
    )
    owner = ArtifactContext(
        tenant_id="tenant-1", user_id="user-1", session_id="s", trace_id="t", run_id="run-1"
    )

    async def scenario() -> None:
        await artifacts.create(
            owner,
            ArtifactCreate(
                type=ArtifactType.REPORT,
                mime_type="application/json",
                content=json.dumps({"status": "pass", "exit_code": 0}).encode(),
                labels={
                    "requirement_id": "coding.tests.run",
                    "evidence_type": "test_run",
                    "input_fingerprint": FINGERPRINT,
                },
            ),
        )
        evaluated = await gate.evaluate(
            resolution,
            context=VerificationContext(
                tenant_id="tenant-1",
                user_id="user-1",
                session_id="session-1",
                trace_id="trace-1",
                run_id="run-1",
                protected_input_fingerprint=FINGERPRINT,
            ),
        )
        assert evaluated.status == CompletionGateStatus.PASS

    run(scenario())
