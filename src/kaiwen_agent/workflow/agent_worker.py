"""Run a configured `AgentRuntime` as a workflow `Worker`.

`AgentRuntimeWorker` adapts the existing runtime loop to the existing `Worker` protocol so
the existing `TaskScheduler` (dependencies, resource keys, concurrency, retries, timeouts,
cancellation) runs it. It adds no model loop, scheduler, policy engine or store.

Everything that confers authority comes from the host that built the worker: permissions,
tool names, the role, and the workspace authorization. The compiled task contributes
identity (re-checked against the durable run) and untrusted plan text, nothing else.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from kaiwen_agent.artifacts import ArtifactManager
from kaiwen_agent.context_engine import (
    ContextBudget,
    ContextBuilder,
    ContextBuildRequest,
    ContextRenderer,
)
from kaiwen_agent.exceptions import ToolNotFoundError
from kaiwen_agent.realtime.conversation import (
    ConversationMessage,
    ConversationModality,
    ConversationRole,
    ConversationStore,
)
from kaiwen_agent.runtime import (
    AgentRuntime,
    AutonomyLevel,
    DurableRunManager,
    RuntimeIdentity,
    WorkspaceBinding,
)
from kaiwen_agent.tasks import AgentTask, TaskStore
from kaiwen_agent.tools.durability import DurableActionScope
from kaiwen_agent.types import AgentResult
from kaiwen_agent.workers import TaskExecutionContext
from kaiwen_agent.workflow.context import (
    _RESERVED_METADATA,
    trusted_action_scope,
    trusted_task_context,
    trusted_task_run,
)
from kaiwen_agent.workflow.controller import ProtectedInputFingerprintProvider
from kaiwen_agent.workflow.lead_tools import (
    LEAD_SESSION_SERVICE,
    LEAD_TOOL_NAMES,
    PROPOSALS_OUTPUT_KEY,
    LeadTools,
    LeadToolSession,
)
from kaiwen_agent.workflow.task_context import (
    CrossCycleHandoffs,
    EvidenceAuthenticator,
    TaskContextAssembler,
    TaskContextSource,
    private_conversation_id,
)

logger = logging.getLogger(__name__)

_TASK_QUERY = "Carry out the assigned task using only the tools you were given."


class WorkerConfigurationError(ValueError):
    """The worker is configured inconsistently; it must not run."""


class WorkspaceBindingError(PermissionError):
    """The durable run is not bound to the workspace this worker is authorized for."""


class WorkspaceAuthorization(Protocol):
    """A host-built authorization for one workspace (see `AuthorizedCodingWorkspace`)."""

    @property
    def binding(self) -> WorkspaceBinding: ...

    @property
    def pipeline(self) -> Any: ...

    def issues(self, *, controller: Any = None) -> list[str]: ...


class AgentRuntimeWorker:
    """`Worker` over one configured `AgentRuntime` for one role."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        runs: DurableRunManager,
        task_store: TaskStore,
        role: str,
        permissions: frozenset[str],
        tool_names: Sequence[str] = (),
        allowed_capabilities: frozenset[str] = frozenset(),
        evidence_tool_names: frozenset[str] = frozenset(),
        workspace: WorkspaceAuthorization | None = None,
        controller: Any = None,
        artifacts: ArtifactManager | None = None,
        fingerprints: ProtectedInputFingerprintProvider | None = None,
        conversations: ConversationStore | None = None,
        private_scope: Literal["task", "role"] = "task",
        autonomy: AutonomyLevel = AutonomyLevel.AUTONOMOUS,
        services: Mapping[str, Any] | None = None,
        context_budget: ContextBudget | None = None,
        max_summary_chars: int = 4_000,
        lead: LeadTools | None = None,
        cross_cycle: CrossCycleHandoffs | None = None,
        evidence_signer: EvidenceAuthenticator | None = None,
    ) -> None:
        if not role or not role.strip():
            raise WorkerConfigurationError("A worker needs a role")
        if not 1 <= max_summary_chars <= 20_000:
            raise WorkerConfigurationError("max_summary_chars must be between 1 and 20000")
        tools = tuple(tool_names)
        for name in tools:
            try:
                runtime.registry.get(name)
            except ToolNotFoundError as error:
                raise WorkerConfigurationError(f"Unknown tool for worker: {name}") from error
        if (LEAD_TOOL_NAMES & set(tools)) and lead is None:
            # The Lead tools need a per-execution session that only `lead=` can supply.
            raise WorkerConfigurationError("Lead tools need the worker to be built with lead=")
        if lead is not None:
            registered = {definition.name for definition in lead.definitions()}
            if not registered <= set(tools):
                raise WorkerConfigurationError("A Lead worker must be given every Lead tool")
            for definition in lead.definitions():
                # The registry must hold this Lead's own tool objects, not a look-alike with
                # another configuration, and the worker must hold the permission they require.
                if runtime.registry.get(definition.name) is not definition:
                    raise WorkerConfigurationError(
                        f"Registered tool {definition.name} is not this worker's Lead tool"
                    )
                if not definition.permissions <= set(permissions):
                    raise WorkerConfigurationError(
                        f"The worker lacks the permission for {definition.name}"
                    )
        if not evidence_tool_names <= set(tools):
            raise WorkerConfigurationError("Evidence tools must be among the worker's tools")
        reserved = _RESERVED_METADATA & set(runtime.metadata)
        if reserved - {"workspace_id"}:
            raise WorkerConfigurationError(
                f"Runtime metadata may not set reserved keys: {sorted(reserved)}"
            )
        if evidence_tool_names:
            # Checked before any model or tool call. A missing controller would skip the
            # fingerprint, signer, artifact and quality-gate agreement checks entirely.
            if workspace is None:
                raise WorkerConfigurationError(
                    "A worker that can produce execution evidence needs an authorized workspace"
                )
            if controller is None:
                raise WorkerConfigurationError(
                    "A worker that can produce execution evidence needs the workflow "
                    "controller whose gate verifies it"
                )
            if workspace.binding.identity_digest is None:
                raise WorkerConfigurationError(
                    "A worker that can produce execution evidence needs a workspace identity"
                )
        if workspace is not None:
            problems = workspace.issues(controller=controller)
            if problems:
                raise WorkerConfigurationError(
                    "Workspace authorization is inconsistent: " + "; ".join(problems)
                )
            configured = runtime.metadata.get("workspace_id")
            if configured is not None and configured != workspace.binding.workspace_id:
                raise WorkerConfigurationError("Runtime metadata names a different workspace")
        workspace_signer = (
            getattr(getattr(workspace, "pipeline", None), "signer", None)
            if workspace is not None
            else None
        )
        if (
            evidence_signer is not None
            and workspace_signer is not None
            and evidence_signer is not workspace_signer
        ):
            raise WorkerConfigurationError(
                "The evidence signer differs from the authorized workspace's signer"
            )
        self.lead = lead
        self.runtime = runtime
        self.runs = runs
        self.role = role.strip()
        self.permissions = frozenset(permissions)
        self.tool_names = tools
        self.allowed_capabilities = frozenset(allowed_capabilities)
        self.evidence_tool_names = frozenset(evidence_tool_names)
        self.workspace = workspace
        self.conversations = conversations
        self.private_scope = private_scope
        self.autonomy = autonomy
        self.services = dict(services or {})
        self.context_budget = context_budget or ContextBudget()
        self.max_summary_chars = max_summary_chars
        try:
            self.assembler = TaskContextAssembler(
                store=task_store,
                artifacts=artifacts,
                fingerprints=fingerprints,
                cross_cycle=cross_cycle,
                evidence_signer=workspace_signer or evidence_signer,
            )
        except ValueError as error:  # e.g. cross-cycle reads another task store
            raise WorkerConfigurationError(str(error)) from error

    async def execute(self, task: AgentTask, context: TaskExecutionContext) -> dict[str, Any]:
        if not task.capabilities <= self.allowed_capabilities:
            raise PermissionError("The task requests capabilities this worker is not allowed")
        record = await trusted_task_run(task, runs=self.runs)
        workspace_id: str | None = None
        if self.workspace is not None:
            if record.workspace_binding != self.workspace.binding:
                raise WorkspaceBindingError(
                    "The durable run is not bound to this worker's authorized workspace"
                )
            workspace_id = self.workspace.binding.workspace_id
        agent_context = await trusted_task_context(
            task,
            runs=self.runs,
            permissions=self.permissions,
            workspace_id=workspace_id,
            role=self.role,
        )
        logical = task.metadata.get("logical_task_id")
        conversation_id = private_conversation_id(
            run_id=record.id,
            role=self.role,
            logical_task_id=logical if isinstance(logical, str) else None,
            scope=self.private_scope,
        )
        identity = RuntimeIdentity(
            session_id=record.session_id,
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            conversation_id=conversation_id,
            trace_id=task.trace_id,
            permissions=self.permissions,
            metadata=dict(agent_context.metadata),
        )
        package = await self.assembler.assemble(
            task,
            run=record,
            identity=identity,
            role=self.role,
            workspace_id=workspace_id,
            objective=_plan_text(task, "objective"),
            acceptance_criteria=_plan_list(task, "acceptance_criteria"),
        )
        prompt = await self._render(package, identity)
        await context.report_progress(0.1, details={"phase": "started", "role": self.role})
        await context.checkpoint(state={"phase": "started", "role": self.role, "task": task.id})
        # A fresh sink per execution: nothing staged by an earlier or concurrent execution (or a
        # cached tool response) can reach this one, and nothing is shared through `self.services`.
        session: LeadToolSession | None = None
        services: Mapping[str, Any] = self.services
        if self.lead is not None:
            session = LeadToolSession(
                run_id=record.id,
                parent_task_id=task.id,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                session_id=record.session_id,
                conversation_id=record.conversation_id,
                workspace_id=workspace_id,
            )
            services = {**self.services, LEAD_SESSION_SERVICE: session}
        result = await self._run(prompt, identity, trusted_action_scope(task, record), services)
        await context.checkpoint(state={"phase": "finished", "role": self.role, "task": task.id})
        await context.report_progress(1.0, details={"phase": "finished", "role": self.role})
        await self._remember(task, identity, result)
        output = self._output(task, record.id, result)
        if session is not None and self.lead is not None:
            # Finalized only after a successful run; the scheduler commits it with COMPLETED.
            output[PROPOSALS_OUTPUT_KEY] = session.finalize(
                result.tool_results, config=self.lead.config
            )
        return output

    async def _render(self, package: Any, identity: RuntimeIdentity) -> str:
        assert identity.tenant_id is not None and identity.user_id is not None
        built = await ContextBuilder([TaskContextSource(package)]).build(
            ContextBuildRequest(
                agent_id=self.role,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                session_id=identity.session_id,
                conversation_id=identity.conversation_id,
                query=_TASK_QUERY,
                capability="action" if self.tool_names else "chat",
                trace_id=identity.trace_id or "trace",
            ),
            budget=self.context_budget,
        )
        return ContextRenderer().render(built)

    async def _run(
        self,
        prompt: str,
        identity: RuntimeIdentity,
        scope: DurableActionScope,
        services: Mapping[str, Any],
    ) -> AgentResult:
        if self.tool_names:
            return await self.runtime.action(
                prompt,
                identity=identity,
                tool_names=self.tool_names,
                autonomy=self.autonomy,
                services=services,
                action_scope=scope,
            )
        return await self.runtime.chat(
            prompt, identity=identity, autonomy=self.autonomy, services=services
        )

    async def _remember(
        self, task: AgentTask, identity: RuntimeIdentity, result: AgentResult
    ) -> None:
        """Record this agent's own exchange in its private conversation, if configured."""

        if self.conversations is None or not result.text.strip():
            return
        assert identity.tenant_id and identity.user_id and identity.conversation_id
        for role, text in (
            (ConversationRole.USER, task.name),
            (ConversationRole.ASSISTANT, result.text),
        ):
            await self.conversations.append_message(
                ConversationMessage(
                    conversation_id=identity.conversation_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    role=role,
                    modality=ConversationModality.TEXT,
                    text=text[:4_000],
                )
            )

    def _output(self, task: AgentTask, run_id: str, result: AgentResult) -> dict[str, Any]:
        evidence: list[str] = []
        blockers: list[str] = []
        calls: list[dict[str, Any]] = []
        for tool_result in result.tool_results[:50]:
            calls.append({"tool": tool_result.name, "is_error": bool(tool_result.is_error)})
            if tool_result.is_error:
                blockers.append(f"tool error: {tool_result.name}")
            if tool_result.name in self.evidence_tool_names and isinstance(
                tool_result.output, dict
            ):
                refs = tool_result.output.get("artifact_refs")
                if isinstance(refs, (list, tuple)):
                    evidence.extend(item for item in refs if isinstance(item, str))
        return {
            "role": self.role,
            "run_id": run_id,
            "task_id": task.id,
            "status": "completed",
            "summary": result.text[: self.max_summary_chars],
            "artifact_refs": [],
            "evidence_refs": list(dict.fromkeys(evidence))[:50],
            "blockers": blockers[:20],
            "follow_up_suggestions": [],
            "tool_calls": calls,
            "usage": result.usage.model_dump(),
            # A model reply is a report. Only the completion gate verifies anything.
            "verified": False,
        }


def _plan_text(task: AgentTask, key: str) -> str:
    value = task.input.get(key)
    return value if isinstance(value, str) else ""


def _plan_list(task: AgentTask, key: str) -> list[str]:
    value = task.input.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
