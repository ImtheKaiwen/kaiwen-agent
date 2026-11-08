"""PR-0 review: tool-result processing keeps executor compatibility and run ownership.

`ToolResultProcessingExecutor` decorates a `ToolExecutor`. It must keep working over executors
written before `action_scope` existed, never drop a durable scope silently, and own offloaded
tool-result artifacts by the durable workflow run (the run `TaskContextAssembler` checks).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from kaiwen_agent.artifacts import ArtifactContext, ArtifactManager
from kaiwen_agent.context import AgentContext
from kaiwen_agent.tasks import TaskStatus
from kaiwen_agent.tools import ToolExecutor
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.tools.durability import DurableActionScope
from kaiwen_agent.tools.results import (
    ArtifactToolResultProcessor,
    PassthroughToolResultProcessor,
    ToolResultProcessingExecutor,
)
from kaiwen_agent.types import ModelResponse, ToolCall, ToolResult
from kaiwen_agent.workflow import TaskContextAssembler, WorkflowOutcomeStatus
from tests.test_agent_runtime_worker import (
    Flow,
    _artifact_context,
    _completed_task,
    _consumer,
    owner,
    plan,
    spec,
)
from tests.test_durable_action_scope import Script, execute

BIG = {"blob": "x" * 200}
RUN = "runtime_run-1"


class Empty(BaseModel):
    pass


def big_tool(*, side_effect: str = "none") -> ToolDefinition:
    async def handler(context: AgentContext, arguments: BaseModel) -> object:
        return BIG

    return ToolDefinition(
        name="demo.write" if side_effect != "none" else "demo.read",
        description="Return a large result.",
        input_model=Empty,
        handler=handler,
        permissions=frozenset({"demo.write"}),
        side_effect=side_effect,  # type: ignore[arg-type]
    )


def context(**metadata: Any) -> AgentContext:
    return AgentContext(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        trace_id="trace-1",
        permissions=frozenset({"demo.write"}),
        metadata=metadata,
    )


def scope(run_id: str = RUN, task_id: str = "task-1") -> DurableActionScope:
    return DurableActionScope(
        runtime_run_id=run_id,
        task_id=task_id,
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
    )


class LegacyExecutor(ToolExecutor):
    """A third-party subclass written against the signature before `action_scope`."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def execute(  # type: ignore[override]
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: AgentContext,
        *,
        execution_scope: str,
        emit: Any = None,
    ) -> ToolResult:
        self.calls += 1
        return ToolResult(call_id=call.id, name=definition.name, output=BIG)


class RecordingProcessor:
    def __init__(self) -> None:
        self.run_ids: list[str] = []

    async def process(
        self, result: ToolResult, *, context: AgentContext, run_id: str
    ) -> ToolResult:
        self.run_ids.append(run_id)
        return result


def read_call() -> ToolCall:
    return ToolCall(id="call-1", name="demo.read", arguments={})


def test_a_legacy_executor_subclass_still_works_without_a_scope() -> None:
    async def scenario() -> None:
        legacy = LegacyExecutor()
        executor = ToolResultProcessingExecutor(legacy, PassthroughToolResultProcessor())
        result = await executor.execute(
            big_tool(), read_call(), context(), execution_scope="run_invocation"
        )
        assert result.output == BIG and legacy.calls == 1

    asyncio.run(scenario())


def test_a_legacy_executor_never_receives_a_scope_silently_dropped() -> None:
    async def scenario() -> None:
        legacy = LegacyExecutor()
        executor = ToolResultProcessingExecutor(legacy, PassthroughToolResultProcessor())
        with pytest.raises(TypeError, match="durable action scope"):
            await executor.execute(
                big_tool(),
                read_call(),
                context(),
                execution_scope="run_invocation",
                action_scope=scope(),
            )
        assert legacy.calls == 0  # the tool did not run without its durable ownership

    asyncio.run(scenario())


def test_a_custom_processor_gets_the_invocation_run_standalone_and_the_durable_run_scoped() -> None:
    async def scenario() -> None:
        processor = RecordingProcessor()
        executor = ToolResultProcessingExecutor(ToolExecutor(), processor)
        await executor.execute(big_tool(), read_call(), context(), execution_scope="run_inv")
        await executor.execute(
            big_tool(),
            read_call(),
            context(),
            execution_scope="run_inv",
            action_scope=scope(),
        )
        assert processor.run_ids == ["run_inv", RUN]

    asyncio.run(scenario())


async def offload(artifacts: ArtifactManager, **options: Any) -> str:
    executor = ToolResultProcessingExecutor(
        ToolExecutor(), ArtifactToolResultProcessor(artifacts, threshold_bytes=16)
    )
    result = await executor.execute(big_tool(), read_call(), context(), **options)
    assert result.output["offloaded"] is True  # type: ignore[index]
    return str(result.output["artifact_id"])  # type: ignore[index]


async def stored_run(artifacts: ArtifactManager, artifact_id: str, run_id: str) -> str | None:
    artifact = await artifacts.read(
        ArtifactContext(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            trace_id="t",
            run_id=run_id,
        ),
        artifact_id,
    )
    return artifact.metadata.run_id


def test_standalone_offloading_is_owned_by_the_invocation_run() -> None:
    async def scenario() -> None:
        artifacts = ArtifactManager()
        artifact_id = await offload(artifacts, execution_scope="run_inv")
        assert await stored_run(artifacts, artifact_id, "run_inv") == "run_inv"

    asyncio.run(scenario())


def test_workflow_offloading_is_owned_by_the_durable_run() -> None:
    async def scenario() -> None:
        artifacts = ArtifactManager()
        artifact_id = await offload(artifacts, execution_scope="run_inv", action_scope=scope())
        assert await stored_run(artifacts, artifact_id, RUN) == RUN

    asyncio.run(scenario())


def test_offloaded_results_flow_through_a_handoff_only_for_the_owning_run(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        flow = Flow(tmp_path)
        artifacts = ArtifactManager()
        run = await flow.new_run()
        other = await flow.new_run()
        record = await flow.runs.start(run_id=run.id, identity=owner())
        mine = await offload(
            artifacts, execution_scope="run_inv", action_scope=scope(run_id=run.id)
        )
        elsewhere = await offload(
            artifacts, execution_scope="run_inv", action_scope=scope(run_id=other.id)
        )
        standalone = await offload(artifacts, execution_scope="run_inv")
        await flow.store.save_task(
            _completed_task("dep", run.id, output={"artifact_refs": [mine, elsewhere, standalone]})
        )
        package = await TaskContextAssembler(store=flow.store, artifacts=artifacts).assemble(
            _consumer(run.id, "dep"), run=record, identity=owner(), role="tester"
        )
        handoff = package.handoffs[0]
        assert handoff.artifact_refs == (mine,)
        assert handoff.unverified_reference_count == 2
        # Ownership is not weakened: the artifact context of another run is no membership.
        assert _artifact_context(other.id).run_id != run.id

    asyncio.run(scenario())


def test_a_workflow_worker_offloads_tool_output_under_its_durable_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        flow = Flow(tmp_path)
        artifacts = ArtifactManager()
        processor = ArtifactToolResultProcessor(artifacts, threshold_bytes=16)
        seen: list[str] = []

        class Spy:
            async def process(self, result, *, context, run_id):  # type: ignore[no-untyped-def]
                seen.append(run_id)
                return await processor.process(result, context=context, run_id=run_id)

        runtime = flow.runtime(
            Script(tool_call_for("demo.write"), ModelResponse(text="done")),  # type: ignore[arg-type]
            tools=(big_tool(side_effect="reversible"),),
            permissions=frozenset({"demo.write"}),
            tool_result_processor=Spy(),
        )
        flow.worker("w", runtime, permissions=frozenset({"demo.write"}), tool_names=("demo.write",))
        run = await flow.new_run()
        outcome = await execute(flow, run, plan(spec("t", "w")))
        assert outcome.status == WorkflowOutcomeStatus.COMPLETED, outcome.reason
        assert outcome.tasks[0].status == TaskStatus.COMPLETED
        assert seen == [run.id]

    asyncio.run(scenario())


def tool_call_for(name: str) -> ModelResponse:
    return ModelResponse(tool_calls=[ToolCall(id="call-1", name=name, arguments={})])
