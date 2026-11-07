from __future__ import annotations

import asyncio
from collections.abc import Sequence
from hashlib import sha256
from typing import Any

from pydantic import BaseModel

from kaiwen_agent.artifacts import ArtifactManager
from kaiwen_agent.context import AgentContext
from kaiwen_agent.control import RunControl
from kaiwen_agent.kits.coding import (
    CodingExecutionBinding,
    CodingKit,
    CodingPreflightMode,
    CodingSimpleAPIError,
    ObjectivePlanner,
    PlanningConstraints,
)
from kaiwen_agent.models import DataLocality, ModelCapabilities, ModelToolStrategy
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.quality import CompletionGate, VerifierRegistry
from kaiwen_agent.runtime import DurableRunManager, RuntimeIdentity, RuntimeRunStatus
from kaiwen_agent.standards import StandardResolution, StandardsRegistry
from kaiwen_agent.tasks import TaskScheduler
from kaiwen_agent.tools import ToolExecutor, ToolRegistry
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import AgentInput, ModelResponse, ToolCall, ToolResult
from kaiwen_agent.workers import FunctionWorker, WorkerRegistry
from kaiwen_agent.workflow import WorkflowCompiler, WorkflowController


class EchoInput(BaseModel):
    value: str


class CapturingProvider:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.tool_names: list[tuple[str, ...]] = []

    async def respond(
        self,
        agent_input: AgentInput,
        *,
        context: AgentContext,
        tools: Sequence[ToolDefinition],
        tool_results: Sequence[ToolResult],
        provider_state: Any | None,
    ) -> ModelResponse:
        del agent_input, context, tool_results, provider_state
        self.tool_names.append(tuple(item.name for item in tools))
        return self.responses.pop(0)


class StaticPlanner:
    async def propose_plan(
        self, *, objective: str, context: dict[str, Any], correction: str | None
    ) -> dict[str, Any]:
        del context, correction
        return {
            "objective": objective,
            "tasks": [{"id": "work", "name": "Work", "worker_id": "worker"}],
        }


def _capabilities(*, locality: DataLocality = DataLocality.CLOUD_ALLOWED) -> ModelCapabilities:
    return ModelCapabilities(
        provider="fake",
        model="test-model",
        context_window=16_384,
        tool_strategy=ModelToolStrategy.NATIVE,
        structured_output=True,
        data_locality=locality,
    )


def _execution(tmp_path, *, provider_tools: ToolRegistry | None = None) -> CodingExecutionBinding:
    store = SQLitePersistence(tmp_path / "simple-api.sqlite3")
    runs = DurableRunManager(store)
    workers = WorkerRegistry()

    async def execute(task, context):
        del task, context
        return {"ok": True}

    workers.register("worker", FunctionWorker(execute))
    compiler = WorkflowCompiler(
        workers=workers,
        worker_allowlist=frozenset({"worker"}),
        effective_capabilities=frozenset(),
        resource_allowlist=frozenset(),
    )
    controller = WorkflowController(
        runs=runs,
        compiler=compiler,
        scheduler_factory=lambda graph: TaskScheduler(
            graph=graph,
            workers=workers,
            store=store,
            control=RunControl(checkpoints=store),
        ),
        completion_gate=CompletionGate(verifiers=VerifierRegistry(), artifacts=ArtifactManager()),
    )
    tools = provider_tools or ToolRegistry()
    return CodingExecutionBinding(
        planner=ObjectivePlanner(
            StaticPlanner(),
            constraints=PlanningConstraints(worker_allowlist=frozenset({"worker"})),
        ),
        controller=controller,
        runs=runs,
        standards=StandardResolution(pack_refs=(), requirements=(), prompt_refs=()),
        standards_registry=StandardsRegistry(),
        protected_input_fingerprint=lambda objective: sha256(objective.encode()).hexdigest(),
        tools=tools,
        tool_executor=ToolExecutor(),
        permissions=frozenset({"echo.use"}),
        action_tools=frozenset({"test.echo"}) if tools.definitions() else frozenset(),
    )


def test_local_chat_is_lazy_tool_free_and_preserves_turkish_preference(tmp_path) -> None:
    created: list[CapturingProvider] = []

    def provider_factory(_: str) -> CapturingProvider:
        provider = CapturingProvider([ModelResponse(text="Türkçe yanıt")])
        created.append(provider)
        return provider

    kit = CodingKit.local(
        workspace=tmp_path,
        provider="fake",
        model="test-model",
        provider_factory=provider_factory,
        user_language="tr",
    )
    assert created == []
    assert kit.preflight().ok
    assert kit.identity.metadata["conversation_preferences"]["user_language"] == "tr"

    async def scenario() -> None:
        async with kit:
            result = await kit.agent().chat("Bu projeyi açıkla")
            assert result.text == "Türkçe yanıt"

    asyncio.run(scenario())
    assert created[0].tool_names == [()]


def test_action_requires_exact_trusted_allowlist_and_never_leaks_tools_to_chat(tmp_path) -> None:
    async def echo(context: AgentContext, arguments: EchoInput) -> dict[str, str]:
        del context
        return {"value": arguments.value}

    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            name="test.echo",
            description="Echo a value.",
            input_model=EchoInput,
            handler=echo,
            permissions=frozenset({"echo.use"}),
        )
    )
    provider = CapturingProvider(
        [
            ModelResponse(text="chat"),
            ModelResponse(
                tool_calls=[ToolCall(id="call-1", name="test.echo", arguments={"value": "x"})]
            ),
            ModelResponse(text="action complete"),
        ]
    )
    kit = CodingKit.local(
        workspace=tmp_path,
        provider="fake",
        model="test-model",
        provider_factory=lambda _: provider,
        model_capabilities=_capabilities(),
        execution=_execution(tmp_path, provider_tools=tools),
    )

    async def scenario() -> None:
        assert (await kit.agent().chat("hello")).text == "chat"
        result = await kit.agent().action("echo", tools=["test.echo"])
        assert result.text == "action complete"

    asyncio.run(scenario())
    assert provider.tool_names == [(), ("test.echo",), ("test.echo",)]
    report = kit.preflight(mode=CodingPreflightMode.ACTION, tool_names=["unknown"])
    assert "tools_not_allowed" in {issue.code for issue in report.issues}


def test_local_only_fails_closed_for_cloud_model(tmp_path) -> None:
    kit = CodingKit.local(
        workspace=tmp_path,
        provider="fake",
        model="test-model",
        provider_factory=lambda _: CapturingProvider([ModelResponse(text="unused")]),
        model_capabilities=_capabilities(locality=DataLocality.CLOUD_ALLOWED),
        data_locality=DataLocality.LOCAL_ONLY,
        execution=_execution(tmp_path),
    )

    report = kit.preflight(mode=CodingPreflightMode.WORKFLOW)

    assert "locality_violation" in {issue.code for issue in report.issues}


def test_workspace_path_does_not_grant_action_authority(tmp_path) -> None:
    kit = CodingKit.local(
        workspace=tmp_path,
        provider="fake",
        model="test-model",
        provider_factory=lambda _: CapturingProvider([ModelResponse(text="unused")]),
    )

    report = kit.preflight(mode=CodingPreflightMode.ACTION, tool_names=["filesystem.read"])

    assert not report.ok
    assert "tools_not_allowed" in {issue.code for issue in report.issues}


def test_unknown_local_provider_error_is_actionable_and_secret_safe(tmp_path) -> None:
    kit = CodingKit.local(
        workspace=tmp_path,
        provider="unknown-provider",
        model="unknown-model",
    )

    async def scenario() -> None:
        try:
            await kit.agent().chat("hello")
        except CodingSimpleAPIError as error:
            assert error.code == "provider_not_registered"
            assert "secret" not in error.message.casefold()
            assert "sk-" not in str(error)
        else:
            raise AssertionError("unknown provider must fail")

    asyncio.run(scenario())


def test_action_preflight_requires_real_approval_gate_and_capabilities(tmp_path) -> None:
    async def write(context: AgentContext, arguments: EchoInput) -> dict[str, str]:
        del context
        return {"value": arguments.value}

    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            name="test.echo",
            description="Approval protected test action.",
            input_model=EchoInput,
            handler=write,
            permissions=frozenset({"echo.use"}),
            side_effect="reversible",
            requires_approval=True,
        )
    )
    execution = _execution(tmp_path, provider_tools=tools)
    kit = CodingKit.local(
        workspace=tmp_path,
        provider="fake",
        model="test-model",
        provider_factory=lambda _: CapturingProvider([ModelResponse(text="unused")]),
        execution=execution,
    )

    report = kit.preflight(mode=CodingPreflightMode.ACTION, tool_names=["test.echo"])

    assert {"approval_required", "provider_capabilities_missing"} <= {
        issue.code for issue in report.issues
    }


def test_workflow_requires_real_sandbox_when_host_marks_it_required(tmp_path) -> None:
    execution = _execution(tmp_path)
    execution = CodingExecutionBinding(
        planner=execution.planner,
        controller=execution.controller,
        runs=execution.runs,
        standards=execution.standards,
        standards_registry=execution.standards_registry,
        protected_input_fingerprint=execution.protected_input_fingerprint,
        requires_sandbox=True,
    )
    kit = CodingKit.local(
        workspace=tmp_path,
        provider="fake",
        model="test-model",
        provider_factory=lambda _: CapturingProvider([ModelResponse(text="unused")]),
        model_capabilities=_capabilities(),
        execution=execution,
    )

    report = kit.preflight(mode=CodingPreflightMode.WORKFLOW)

    assert "sandbox_missing" in {issue.code for issue in report.issues}


def test_workflow_handle_uses_durable_run_and_rejects_cross_owner_resume(tmp_path) -> None:
    execution = _execution(tmp_path)
    kit = CodingKit.local(
        workspace=tmp_path,
        provider="fake",
        model="test-model",
        provider_factory=lambda _: CapturingProvider([ModelResponse(text="unused")]),
        model_capabilities=_capabilities(),
        execution=execution,
    )

    async def scenario() -> None:
        handle = await kit.agent().start("Complete one bounded task")
        result = await handle.wait(timeout=2)
        assert result.status is RuntimeRunStatus.COMPLETED
        assert result.tasks_total == 1
        assert result.tasks_completed == 1
        assert (await handle.status()).id == handle.id

        restarted_kit = CodingKit.local(
            workspace=tmp_path,
            provider="fake",
            model="test-model",
            provider_factory=lambda _: CapturingProvider([ModelResponse(text="unused")]),
            model_capabilities=_capabilities(),
            execution=_execution(tmp_path),
        )
        assert restarted_kit.identity.session_id == kit.identity.session_id
        reconnected = await restarted_kit.resume(handle.id)
        assert reconnected.id == handle.id
        assert (await reconnected.wait(timeout=1)).status is RuntimeRunStatus.COMPLETED

        wrong_owner = CodingKit.for_host(
            identity=RuntimeIdentity(
                session_id=kit.identity.session_id,
                tenant_id="other-tenant",
                user_id="other-user",
            ),
            workspace=tmp_path,
            agent_factory=kit.agent_factory,
            provider_ref=kit.provider_ref,
            execution=execution,
            model_capabilities=kit.model_capabilities,
            provider_preset=kit.provider_preset,
        )
        try:
            await wrong_owner.resume(handle.id)
        except KeyError:
            pass
        else:
            raise AssertionError("cross-owner run resume must fail closed")

        other_workspace = tmp_path / "other-workspace"
        other_workspace.mkdir()
        wrong_workspace = CodingKit.for_host(
            identity=kit.identity,
            workspace=other_workspace,
            agent_factory=kit.agent_factory,
            provider_ref=kit.provider_ref,
            execution=execution,
            model_capabilities=kit.model_capabilities,
            provider_preset=kit.provider_preset,
        )
        try:
            await wrong_workspace.resume(handle.id)
        except CodingSimpleAPIError as error:
            assert error.code == "resume_configuration_mismatch"
        else:
            raise AssertionError("cross-workspace run resume must fail closed")

    asyncio.run(scenario())


def test_pause_and_cancel_are_explicitly_unsupported_without_host_control(tmp_path) -> None:
    kit = CodingKit.local(
        workspace=tmp_path,
        provider="fake",
        model="test-model",
        provider_factory=lambda _: CapturingProvider([ModelResponse(text="unused")]),
        model_capabilities=_capabilities(),
        execution=_execution(tmp_path),
    )

    async def scenario() -> None:
        handle = await kit.agent().start("Complete one bounded task")
        await handle.wait(timeout=2)
        for operation, code in (
            (handle.pause, "pause_unsupported"),
            (handle.cancel, "cancel_unsupported"),
        ):
            try:
                await operation()
            except CodingSimpleAPIError as error:
                assert error.code == code
            else:
                raise AssertionError(f"{code} must be explicit")

    asyncio.run(scenario())
