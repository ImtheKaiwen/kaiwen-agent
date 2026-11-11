"""Model-facing Lead tools and durable delegation proposals (ADR 0015, M6.3.3-C2).

Two registered tools let a Lead agent coordinate without gaining authority:

- `lead.view` returns a bounded, paginated page of the existing `LeadView`. Tenant, user, session,
  run and workspace come from the trusted per-execution `LeadToolSession`, never from arguments.
- `lead.propose_task` stages one bounded task specification in the execution-local sink of that
  session. Staging is not committing: the result never says a proposal is durable.

The commit boundary is the Lead task's own completion. `AgentRuntimeWorker` finalizes the sink after
a successful run and returns the canonical proposal set under `output["delegation_proposals"]`; the
existing `TaskScheduler` then persists that output together with the COMPLETED status in one task
write (through the fenced task store in fenced mode, so a stale owner cannot commit it). A Lead that
fails, is cancelled or crashes never produced a finalized set: its sink is simply discarded.

`DurableLeadProposalSource` is the host-controlled `WorkflowDelegationSource`. At the cycle
boundary it re-reads the completed Lead task from authoritative storage, validates identity,
bounds, host role bindings and the digest, and returns a `DelegationCandidate`. It creates,
schedules and persists nothing: the controller still performs the fresh final admission.

Trust. The model chooses only plan-authored fields (logical id, text, role label, criteria,
capability labels, resource keys, dependencies). The host maps a role label to a worker id and a
capability ceiling (`LeadRoleBinding`); the run, owner, parent, workspace, depth and every
permission come from host records. A stored proposal set is untrusted input at read time and
grants nothing. Its digest detects truncation and accidental corruption; it is not a signature
and does not defend against a writer that is already trusted to write tasks (ADR 0015).

Non-guarantees. Proposal identity makes staging and reading idempotent. It does not make worker
execution exactly-once, reserve quotas, or resume a workflow after a restart.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kaiwen_agent.context import AgentContext
from kaiwen_agent.runtime import DurableRunManager, RuntimeIdentity, WorkspaceBinding
from kaiwen_agent.tasks import AgentTask, TaskStatus, TaskStore
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import ToolResult
from kaiwen_agent.workflow.context import trusted_task_run
from kaiwen_agent.workflow.delegation import DelegationCandidate, WorkflowDelegationProposal
from kaiwen_agent.workflow.lead_view import LeadViewAssembler, LeadViewError
from kaiwen_agent.workflow.models import WorkflowRevisionRequest, WorkflowTaskSpec

logger = logging.getLogger(__name__)

LEAD_VIEW_TOOL = "lead.view"
LEAD_PROPOSE_TOOL = "lead.propose_task"
LEAD_TOOL_NAMES = frozenset({LEAD_VIEW_TOOL, LEAD_PROPOSE_TOOL})
LEAD_VIEW_PERMISSION = "lead.view"
LEAD_PROPOSE_PERMISSION = "lead.propose"
# Key under which the worker places the per-execution session in the tool services.
LEAD_SESSION_SERVICE = "kaiwen.lead_session"
# Key of the committed proposal set inside a Lead task's output.
PROPOSALS_OUTPUT_KEY = "delegation_proposals"
RECORD_VERSION = 1

MAX_VIEW_PAGE = 50
MAX_VIEW_OFFSET = 500
_ID = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"
_ROLE = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
# Element-level limits, conservative against `WorkflowCompiler` (task ids are bounded to 200 and
# patterned; capability and resource labels are free text there, bounded here).
_LABEL = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$"
_RESOURCE = r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,199}$"
Dependency = Annotated[str, Field(min_length=1, max_length=200, pattern=_ID)]
Capability = Annotated[str, Field(min_length=1, max_length=100, pattern=_LABEL)]
ResourceKey = Annotated[str, Field(min_length=1, max_length=200, pattern=_RESOURCE)]
Criterion = Annotated[str, Field(min_length=1, max_length=500)]
_MAX_TASK_BYTES = 8_000
_MAX_RECORD_BYTES = 128_000
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class LeadProposalError(PermissionError):
    """A proposal set or a Lead tool call cannot be trusted. `code` is stable and non-sensitive."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class LeadRoleBinding:
    """Host-owned mapping of one specialist role label to a worker and its capability ceiling."""

    role: str
    worker_id: str
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not re.fullmatch(_ROLE, self.role):
            raise ValueError("A role binding needs a lowercase role label")
        if not self.worker_id or len(self.worker_id) > 200:
            raise ValueError("A role binding needs a worker id")
        if not isinstance(self.capabilities, frozenset) or not all(
            isinstance(item, str) and item for item in self.capabilities
        ):
            raise ValueError("A role binding needs a frozenset of capability labels")


class ProposedTask(BaseModel):
    """One model-proposed specialist task. Every field is untrusted, bounded, plan-authored text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200, pattern=_ID)
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=2_000)
    role: str = Field(pattern=_ROLE)
    acceptance_criteria: tuple[Criterion, ...] = Field(default=(), max_length=10)
    dependencies: tuple[Dependency, ...] = Field(default=(), max_length=10)
    capabilities: tuple[Capability, ...] = Field(default=(), max_length=10)
    resource_keys: tuple[ResourceKey, ...] = Field(default=(), max_length=5)


class ProposeTaskArguments(ProposedTask):
    """Arguments of `lead.propose_task`: exactly a `ProposedTask` and nothing else."""


class LeadViewArguments(BaseModel):
    """Arguments of `lead.view`: a page window only. There is deliberately no run or owner field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    offset: int = Field(default=0, ge=0, le=MAX_VIEW_OFFSET)
    limit: int = Field(default=20, ge=1, le=MAX_VIEW_PAGE)


@dataclass(frozen=True, slots=True)
class LeadToolConfig:
    """Host configuration shared by the tools and the durable source."""

    roles: Mapping[str, LeadRoleBinding]
    resource_allowlist: frozenset[str] = frozenset()
    max_tasks: int = 10

    def __post_init__(self) -> None:
        if not 1 <= self.max_tasks <= 50:
            raise ValueError("max_tasks must be between 1 and 50")
        for role, binding in self.roles.items():
            if not isinstance(binding, LeadRoleBinding) or binding.role != role:
                raise ValueError("Each role binding must be keyed by its own role")
        object.__setattr__(self, "roles", MappingProxyType(dict(self.roles)))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_task(task: ProposedTask) -> dict[str, Any]:
    """Order-insensitive canonical form: sets are sorted, criteria keep their order."""

    return {
        "id": task.id,
        "name": task.name,
        "description": task.description,
        "role": task.role,
        "acceptance_criteria": list(task.acceptance_criteria),
        "dependencies": sorted(set(task.dependencies)),
        "capabilities": sorted(set(task.capabilities)),
        "resource_keys": sorted(set(task.resource_keys)),
    }


class LeadToolSession:
    """Trusted identity plus the staging sink of exactly one Lead task execution.

    Built by `AgentRuntimeWorker` for every `execute` call from the verified durable run and the
    task, never shared between executions and never stored in a long-lived services mapping.
    """

    def __init__(
        self,
        *,
        run_id: str,
        parent_task_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        conversation_id: str | None,
        workspace_id: str | None,
    ) -> None:
        self.run_id = run_id
        self.parent_task_id = parent_task_id
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.session_id = session_id
        self.conversation_id = conversation_id
        self.workspace_id = workspace_id
        self._staged: dict[str, tuple[str, dict[str, Any]]] = {}
        self._sealed = False

    @property
    def sealed(self) -> bool:
        return self._sealed

    def proposal_id(self, canonical: Mapping[str, Any]) -> str:
        """Identity from trusted context and canonical content; never from a tool-call id."""

        material = _canonical(
            [
                "lead-proposal",
                RECORD_VERSION,
                self.run_id,
                self.parent_task_id,
                self.tenant_id,
                self.user_id,
                self.session_id,
                self.workspace_id,
                canonical,
            ]
        )
        return sha256(material.encode()).hexdigest()

    def stage(self, task: ProposedTask, config: LeadToolConfig) -> tuple[str, str, bool]:
        """Stage one task: `(status, code or proposal id, duplicate)`; no raise for model input."""

        if self._sealed:
            return "rejected", "sink_sealed", False
        binding = config.roles.get(task.role)
        if binding is None:
            return "rejected", "unknown_role", False
        if not set(task.capabilities) <= binding.capabilities:
            return "rejected", "capability_not_allowed", False
        if not set(task.resource_keys) <= config.resource_allowlist:
            return "rejected", "resource_not_allowed", False
        if len(set(task.dependencies)) != len(task.dependencies) or task.id in task.dependencies:
            return "rejected", "invalid_dependencies", False
        if len(set(task.capabilities)) != len(task.capabilities) or len(
            set(task.resource_keys)
        ) != len(task.resource_keys):
            return "rejected", "duplicate_values", False
        canonical = canonical_task(task)
        if len(_canonical(canonical).encode()) > _MAX_TASK_BYTES:
            return "rejected", "task_too_large", False
        known = self._staged.get(task.id)
        identity = self.proposal_id(canonical)
        if known is not None:
            if known[0] == identity:
                return "staged", identity, True
            return "rejected", "logical_id_conflict", False
        if len(self._staged) >= config.max_tasks:
            return "rejected", "too_many_proposals", False
        if any(dep not in self._staged for dep in task.dependencies):
            # Dependencies are within this proposal and must be proposed first: no forward
            # references and therefore no cycles.
            return "rejected", "unknown_dependency", False
        self._staged[task.id] = (identity, canonical)
        return "staged", identity, False

    def finalize(self, results: Sequence[ToolResult], *, config: LeadToolConfig) -> dict[str, Any]:
        """Seal the sink and return the canonical set for the task output.

        A staged result reported to the model during this execution whose proposal is not in this
        sink (for example a cached success from an earlier execution) fails the finalization, so a
        proposal can never silently disappear from the committed set.
        """

        self._sealed = True
        reported = {
            result.output["proposal_id"]
            for result in results
            if result.name == LEAD_PROPOSE_TOOL
            and not result.is_error
            and isinstance(result.output, dict)
            and result.output.get("status") == "staged"
            and isinstance(result.output.get("proposal_id"), str)
        }
        held = {identity for identity, _ in self._staged.values()}
        if not reported <= held:
            raise LeadProposalError("staged_proposal_missing")
        tasks = [canonical for _, canonical in sorted(self._staged.values(), key=_by_id)]
        return build_record(
            run_id=self.run_id,
            parent_task_id=self.parent_task_id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            session_id=self.session_id,
            workspace_id=self.workspace_id,
            tasks=tasks,
            config=config,
        )


def _by_id(item: tuple[str, dict[str, Any]]) -> str:
    return str(item[1]["id"])


def _digest(body: Mapping[str, Any]) -> str:
    return sha256(_canonical(body).encode()).hexdigest()


def build_record(
    *,
    run_id: str,
    parent_task_id: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    workspace_id: str | None,
    tasks: list[dict[str, Any]],
    config: LeadToolConfig,
) -> dict[str, Any]:
    if len(tasks) > config.max_tasks:
        raise LeadProposalError("too_many_proposals")
    body: dict[str, Any] = {
        "version": RECORD_VERSION,
        "run_id": run_id,
        "parent_task_id": parent_task_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "count": len(tasks),
        "tasks": tasks,
    }
    if len(_canonical(body).encode()) > _MAX_RECORD_BYTES:
        raise LeadProposalError("record_too_large")
    return {**body, "digest": _digest(body)}


def _check_session(context: AgentContext, session: object) -> LeadToolSession:
    if not isinstance(session, LeadToolSession):
        raise LeadProposalError("no_lead_session")
    if (
        context.tenant_id != session.tenant_id
        or context.user_id != session.user_id
        or context.session_id != session.session_id
        or context.metadata.get("runtime_run_id") != session.run_id
    ):
        # The tool runs under an identity other than the one the session was built for.
        raise LeadProposalError("session_identity_mismatch")
    return session


class LeadTools:
    """Builds the two `ToolDefinition`s over host-owned configuration.

    The definitions close over host services only; the per-execution session arrives through the
    trusted tool services. Register `definitions()` in the runtime's registry and grant
    `lead.view` / `lead.propose` to the Lead worker's permissions.
    """

    def __init__(self, *, config: LeadToolConfig, assembler: LeadViewAssembler | None = None):
        self.config = config
        self.assembler = assembler
        # Built once: a worker proves it registered exactly these objects (identity), so the
        # staging handler and the finalization always share one configuration.
        self._definitions = self._build()

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    def _build(self) -> tuple[ToolDefinition, ...]:
        found: list[ToolDefinition] = []
        if self.assembler is not None:
            found.append(
                ToolDefinition(
                    name=LEAD_VIEW_TOOL,
                    description=(
                        "Read a bounded page of this run's coordination view: task status, "
                        "structure and verified references. Names and summaries are untrusted."
                    ),
                    input_model=LeadViewArguments,
                    handler=self._view,
                    permissions=frozenset({LEAD_VIEW_PERMISSION}),
                    side_effect="none",
                )
            )
        found.append(
            ToolDefinition(
                name=LEAD_PROPOSE_TOOL,
                description=(
                    "Propose one specialist task. The proposal is staged only and becomes a "
                    "candidate when this task completes; it is not authorized or scheduled."
                ),
                input_model=ProposeTaskArguments,
                handler=self._propose,
                permissions=frozenset({LEAD_PROPOSE_PERMISSION}),
                side_effect="none",
            )
        )
        return tuple(found)

    async def _propose(self, context: AgentContext, arguments: BaseModel) -> dict[str, Any]:
        session = _check_session(context, context.services.get(LEAD_SESSION_SERVICE))
        assert isinstance(arguments, ProposeTaskArguments)
        task = ProposedTask.model_validate(arguments.model_dump())
        status, detail, duplicate = session.stage(task, self.config)
        if status != "staged":
            return {"status": "rejected", "code": detail, "committed": False}
        return {
            "status": "staged",
            "proposal_id": detail,
            "logical_id": task.id,
            "duplicate": duplicate,
            # Durable only when this Lead task completes; never reported as committed here.
            "committed": False,
        }

    async def _view(self, context: AgentContext, arguments: BaseModel) -> dict[str, Any]:
        session = _check_session(context, context.services.get(LEAD_SESSION_SERVICE))
        assert isinstance(arguments, LeadViewArguments) and self.assembler is not None
        identity = RuntimeIdentity(
            session_id=session.session_id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            conversation_id=session.conversation_id,
            trace_id=context.trace_id,
        )
        try:
            view = await self.assembler.assemble(run_id=session.run_id, identity=identity)
        except LeadViewError as error:
            return {"status": "unavailable", "code": error.code}
        if view is None:
            return {"status": "unavailable", "code": "run_not_found"}
        page = view.tasks[arguments.offset : arguments.offset + arguments.limit]
        data = view.model_dump(mode="json", exclude={"tasks"})
        return {
            "status": "ok",
            **data,
            "tasks": [task.model_dump(mode="json") for task in page],
            "page": {
                "offset": arguments.offset,
                "limit": arguments.limit,
                "returned": len(page),
                "total_listed": len(view.tasks),
                "has_more": arguments.offset + len(page) < len(view.tasks),
            },
        }


class DurableLeadProposalSource:
    """`WorkflowDelegationSource` that reads committed proposals from completed Lead tasks.

    Reads only; it never writes a task, schedules, or decides admission. Every failure to
    establish a clean record raises `LeadProposalError`, which the controller turns into a
    BLOCKED run (`delegation_source_failed`) rather than ignoring the proposals.
    """

    def __init__(
        self,
        *,
        tasks: TaskStore,
        runs: DurableRunManager,
        config: LeadToolConfig,
        lead_workers: frozenset[str],
        expected_workspace: WorkspaceBinding | None = None,
    ) -> None:
        if not lead_workers:
            raise ValueError("A durable proposal source needs at least one Lead worker id")
        self.tasks = tasks
        self.runs = runs
        self.config = config
        self.lead_workers = frozenset(lead_workers)
        self.expected_workspace = expected_workspace

    async def propose(self, request: WorkflowRevisionRequest) -> DelegationCandidate | None:
        boundary = request.delegation
        if boundary is None:
            raise LeadProposalError("not_a_delegation_boundary")
        found: list[DelegationCandidate] = []
        for parent in boundary.eligible_parents:
            if parent.worker_id not in self.lead_workers:
                continue
            candidate = await self._candidate(request, parent.task_id, parent.worker_id)
            if candidate is not None:
                found.append(candidate)
        if len(found) > 1:
            # One round admits one parent's set; silently picking one would drop the other.
            _log("multiple_proposal_sets", request.run_id)
            raise LeadProposalError("multiple_proposal_sets")
        return found[0] if found else None

    async def _candidate(
        self, request: WorkflowRevisionRequest, task_id: str, worker_id: str
    ) -> DelegationCandidate | None:
        failure = "record_unavailable"
        try:
            task = await self.tasks.get_task(task_id)
        except Exception:  # noqa: BLE001 - storage detail must not escape
            _log(failure, request.run_id)
            raise LeadProposalError(failure) from None
        record = await self._verified_record(request, task, task_id, worker_id)
        if record["count"] == 0:
            return None
        specs = [self._spec(item, record) for item in record["tasks"]]
        try:
            proposal = WorkflowDelegationProposal(tasks=tuple(specs))
        except ValidationError:
            raise LeadProposalError("record_invalid") from None
        return DelegationCandidate(task_id, proposal)

    async def _verified_record(
        self, request: WorkflowRevisionRequest, task: AgentTask | None, task_id: str, worker: str
    ) -> dict[str, Any]:
        run_id = request.run_id
        if task is None or task.id != task_id:
            _log("parent_not_found", run_id)
            raise LeadProposalError("parent_not_found")
        if task.status is not TaskStatus.COMPLETED or task.agent_id != worker:
            raise LeadProposalError("parent_not_eligible")
        if (
            task.metadata.get("runtime_run_id") != run_id
            or task.metadata.get("workflow_cycle") != request.cycle
        ):
            raise LeadProposalError("parent_not_current")
        try:
            run = await trusted_task_run(task, runs=self.runs)
        except Exception:  # noqa: BLE001 - TaskIdentityError or storage; fail closed, no detail
            _log("identity_unverified", run_id)
            raise LeadProposalError("identity_unverified") from None
        if self.expected_workspace is not None and run.workspace_binding != self.expected_workspace:
            raise LeadProposalError("workspace_mismatch")
        workspace_id = run.workspace_binding.workspace_id if run.workspace_binding else None
        output = task.output
        raw = output.get(PROPOSALS_OUTPUT_KEY) if isinstance(output, dict) else None
        if raw is None:
            # A Lead that completed with no record is unavailable, not "no proposal".
            _log("record_missing", run_id)
            raise LeadProposalError("record_missing")
        return self._validate(
            raw,
            run_id=run.id,
            task=task,
            workspace_id=workspace_id,
        )

    def _validate(
        self, raw: object, *, run_id: str, task: AgentTask, workspace_id: str | None
    ) -> dict[str, Any]:
        def invalid(code: str) -> LeadProposalError:
            _log(code, run_id)
            return LeadProposalError(code)

        if not isinstance(raw, dict) or raw.get("version") != RECORD_VERSION:
            raise invalid("record_invalid")
        try:
            size = len(_canonical(raw).encode())
        except (TypeError, ValueError):
            raise invalid("record_invalid") from None
        if size > _MAX_RECORD_BYTES:
            raise invalid("record_too_large")
        digest = raw.get("digest")
        body = {key: value for key, value in raw.items() if key != "digest"}
        if not isinstance(digest, str) or not _HEX64.match(digest) or digest != _digest(body):
            raise invalid("digest_mismatch")
        expected = {
            "run_id": run_id,
            "parent_task_id": task.id,
            "tenant_id": task.tenant_id,
            "user_id": task.user_id,
            "session_id": task.session_id,
            "workspace_id": workspace_id,
        }
        if any(raw.get(key) != value for key, value in expected.items()):
            raise invalid("record_binding_mismatch")
        tasks = raw.get("tasks")
        count = raw.get("count")
        if not isinstance(tasks, list) or count != len(tasks):
            raise invalid("record_truncated")
        if len(tasks) > self.config.max_tasks:
            raise invalid("too_many_proposals")
        ids = [item.get("id") if isinstance(item, dict) else None for item in tasks]
        if len(set(ids)) != len(ids):
            raise invalid("duplicate_logical_id")
        return {**raw, "tasks": tasks}

    def _spec(self, item: object, record: Mapping[str, Any]) -> WorkflowTaskSpec:
        try:
            task = ProposedTask.model_validate(item)
        except ValidationError:
            raise LeadProposalError("record_invalid") from None
        if _canonical(canonical_task(task)) != _canonical(item):
            raise LeadProposalError("record_not_canonical")
        if len(_canonical(item).encode()) > _MAX_TASK_BYTES:
            raise LeadProposalError("task_too_large")
        binding = self.config.roles.get(task.role)
        if binding is None:
            raise LeadProposalError("unknown_role")
        if not set(task.capabilities) <= binding.capabilities:
            raise LeadProposalError("capability_not_allowed")
        if not set(task.resource_keys) <= self.config.resource_allowlist:
            raise LeadProposalError("resource_not_allowed")
        return WorkflowTaskSpec(
            id=task.id,
            name=task.name,
            description=task.description,
            worker_id=binding.worker_id,
            dependencies=task.dependencies,
            input={
                "objective": task.description or task.name,
                "acceptance_criteria": list(task.acceptance_criteria),
                "proposed_by": record["parent_task_id"],
            },
            resource_keys=task.resource_keys,
            capabilities=frozenset(task.capabilities),
        )


def _log(code: str, run_id: str) -> None:
    logger.warning("lead proposal source failure code=%s run=%s", code, run_id)
