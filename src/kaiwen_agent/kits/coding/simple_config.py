from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from kaiwen_agent.events import EventSink, ScopedEventStore
from kaiwen_agent.kits.coding.planner import ObjectivePlanner
from kaiwen_agent.kits.coding.providers import ProviderCompatibility
from kaiwen_agent.kits.coding.team import ConfiguredCodingTeam
from kaiwen_agent.runtime import (
    DurableRunManager,
    RuntimeIdentity,
    RuntimeRunRecord,
)
from kaiwen_agent.sessions import RunStore
from kaiwen_agent.standards import StandardResolution, StandardsRegistry
from kaiwen_agent.teams import TeamRuntime
from kaiwen_agent.tools import ToolExecutor, ToolRegistry
from kaiwen_agent.workflow import WorkflowController, WorkflowOutcome, WorkflowPlan


class CodingPreflightMode(StrEnum):
    CHAT = "chat"
    ACTION = "action"
    WORKFLOW = "workflow"
    TEAM = "team"


class CodingSimpleAPIError(RuntimeError):
    """Stable, secret-safe failure raised by the optional Simple Coding API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class CodingRunControl(Protocol):
    """Host control port backed by the existing scheduler/job control boundary."""

    async def pause(
        self,
        *,
        run_id: str,
        identity: RuntimeIdentity,
        safe_boundary: str,
    ) -> None: ...

    async def cancel(self, *, run_id: str, identity: RuntimeIdentity) -> None: ...

    async def resume(
        self,
        *,
        run: RuntimeRunRecord,
        identity: RuntimeIdentity,
    ) -> WorkflowOutcome: ...


class CodingTeamLauncher(Protocol):
    """Adapter that must execute a team through the existing TeamRuntime/JobManager."""

    async def launch(
        self,
        *,
        configured: ConfiguredCodingTeam,
        runtime: TeamRuntime,
        controller: WorkflowController,
        run: RuntimeRunRecord,
        identity: RuntimeIdentity,
        plan: WorkflowPlan,
        standards: StandardResolution,
        protected_input_fingerprint: str,
    ) -> WorkflowOutcome: ...


@dataclass(frozen=True, slots=True)
class CodingTeamBinding:
    configured: ConfiguredCodingTeam
    runtime: TeamRuntime
    launcher: CodingTeamLauncher
    provider_compatibility: Mapping[str, ProviderCompatibility] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CodingExecutionBinding:
    """Trusted host composition for actions and durable coding workflows.

    Every field points at an existing platform component. This object grants no
    authority by itself; the supplied tools, executor, policy, approvals, workers,
    controller, and stores remain authoritative.
    """

    planner: ObjectivePlanner
    controller: WorkflowController
    runs: DurableRunManager
    standards: StandardResolution
    standards_registry: StandardsRegistry
    protected_input_fingerprint: Callable[[str], str]
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    tool_executor: ToolExecutor = field(default_factory=ToolExecutor)
    permissions: frozenset[str] = frozenset()
    action_tools: frozenset[str] = frozenset()
    planning_context: Mapping[str, Any] = field(default_factory=dict)
    # Kept structural so importing the simple facade does not import optional
    # command/toolkit dependencies. The trusted host owns the concrete runner.
    sandbox: object | None = None
    requires_sandbox: bool = False
    event_sink: EventSink | None = None
    event_store: ScopedEventStore | None = None
    run_store: RunStore | None = None
    control: CodingRunControl | None = None
    team: CodingTeamBinding | None = None

    def __post_init__(self) -> None:
        if self.controller.runs is not self.runs:
            raise ValueError("controller and execution binding must share DurableRunManager")
        registered = {definition.name for definition in self.tools.definitions()}
        missing = self.action_tools - registered
        if missing:
            raise ValueError(f"Action tools are not registered: {sorted(missing)}")
        for name in self.action_tools:
            definition = self.tools.get(name)
            if not definition.permissions <= self.permissions:
                raise ValueError(f"Execution permissions cannot run configured tool: {name}")
