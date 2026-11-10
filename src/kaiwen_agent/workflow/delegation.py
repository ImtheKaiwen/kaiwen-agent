"""Workflow delegation admission: validate a Lead's proposal without acting on it (ADR 0015).

`WorkflowDelegationAdmission.evaluate` answers "would this proposal be admitted now" for a durable
workflow run. It is the workflow-facing counterpart of `SubagentDelegator`'s admission: both share
`DelegationAdmission` (central `PolicyEngine`, capability subsets, resource allowlist), and they
share no submission or execution mechanism. Job-specific checks (job ownership, the job store)
stay in `SubagentDelegator`; the checks here are workflow-specific.

It reads: the durable run (owner, session, running, workspace binding), the run's attested tasks
through `TaskRunQuery`, and the compiler's allowlists and limits. It calls the pure
`WorkflowCompiler.compile` as a dry run to reuse its structural validation. It never creates a
task, writes a proposal, revises a plan, starts the scheduler, submits a job or touches approvals.
The only writes are whatever the central `PolicyEngine` itself records for its own audit trail.

Trust. The run comes from the durable store for the host-supplied identity. The parent task is
found in the run's attested listing, so its identity (`agent_id`, `delegation_depth`) is a host
record; the child's depth is the parent's plus one, computed by the host. `parent_task_id` must
come from trusted context, never from the proposal. The proposal holds only plan-authored task
specs; it has no field for owner, depth, permissions or authorization, and unknown fields are
rejected.

Delegator authority. A parent task's recorded `capabilities` are what a plan asked for: the
compiler checks them only against the host-wide capability set, and an `AgentRuntimeWorker`
checks them against its own `allowed_capabilities` only when it executes the task. They are not
proof that the parent's worker may delegate, or may hand those capabilities down. Whether a
worker may delegate at all, and the ceiling of what it may hand down, come only from the
host-owned `delegators` mapping of `DelegationAuthority` entries, keyed by the parent task's
worker id and bound to that worker's role. A parent whose worker has no entry is refused even
when the central policy allows the target worker: the delegate allowlist (who may be delegated
to) is distinct from the caller's permission to delegate. Tool permissions and capability
labels are separate contracts (`AgentRuntimeWorker.permissions` versus `allowed_capabilities`,
with no enforced relation), so neither is used as the ceiling. A delegated parent's recorded
labels (depth of at least one, set by an earlier admission) additionally narrow its children,
as on the job path; they never widen anything.

Limits of the answer (ADR 0015, 4.2). The result is preliminary and point-in-time: nothing is
reserved, concurrent evaluations can all be admitted, and `WorkflowCompiler` alone is not a
final authorization because it neither evaluates the policy nor knows live delegation quotas.
The final, fresh authorization belongs to the controller integration (PR-C1). Per-run counters
here are per run: the job path counts a caller's active jobs across runs, this path cannot.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.policy import (
    AdmissionDecision,
    AdmissionOutcome,
    DelegationAdmission,
    DelegationClaim,
)
from kaiwen_agent.runtime import (
    DurableRunManager,
    RuntimeIdentity,
    RuntimeRunRecord,
    RuntimeRunStatus,
    WorkspaceBinding,
)
from kaiwen_agent.tasks import AgentTask, TaskRunQuery, TaskStatus
from kaiwen_agent.workers import WorkerNotFoundError
from kaiwen_agent.workflow.compiler import WorkflowCompiler, WorkflowPlanError
from kaiwen_agent.workflow.models import WorkflowPlan, WorkflowRevisionRequest, WorkflowTaskSpec
from kaiwen_agent.workflow.run_tasks import RunTaskScanError, scan_run_tasks

logger = logging.getLogger(__name__)

_MAX_SCAN = 1_000
_DRY_RUN_OBJECTIVE = "delegation admission dry run"

_REASONS = {
    "invalid_trusted_state": "Delegation facts are missing or malformed",
    "state_unavailable": "Trusted workflow state could not be read",
    "state_incomplete": "Trusted workflow state is incomplete, so counters cannot be established",
    "run_not_found": "The workflow run was not found for this owner",
    "run_not_active": "The workflow run is not running",
    "workspace_mismatch": "The workflow run is bound to a different workspace",
    "plan_too_large": "The proposal exceeds the task limit",
    "parent_not_found": "The parent task was not found in this run",
    "parent_not_active": "The parent task is not in a state that may delegate",
    "delegator_not_authorized": "The parent task's worker is not authorized to delegate",
    "delegator_role_mismatch": "The parent task's worker does not hold the authorized role",
    "worker_not_allowed": "A proposed worker is not allowlisted",
    "capability_unavailable": "A proposed capability is not available to the host",
    "worker_capability_mismatch": "A proposed worker is not allowed the requested capabilities",
    "resource_not_allowed": "Requested resource keys are not allowlisted",
    "plan_invalid": "The proposed plan is not valid",
}


@dataclass(frozen=True, slots=True)
class DelegationAuthority:
    """Host-owned permission for one worker to delegate, and the ceiling of what it may hand down.

    The host writes this when it composes the workflow (for example from the members of a
    `TeamDefinition` with `may_delegate`); it is never read from a plan, a proposal or a stored
    task. `worker_id` is the id the worker is registered under (a parent task's `agent_id`),
    `role` is the host-assigned role it must hold (checked against the registered worker's own
    `role` when it has one, as `AgentRuntimeWorker` does), and `capabilities` is the delegation
    ceiling: no child may carry a capability outside it. It is a capability-label ceiling, not a
    tool-permission set; the two are distinct contracts and are not converted into each other.
    """

    worker_id: str
    role: str
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.worker_id, str) or not self.worker_id:
            raise ValueError("A delegation authority needs a worker id")
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("A delegation authority needs a role")
        if not isinstance(self.capabilities, frozenset) or not all(
            isinstance(item, str) and item for item in self.capabilities
        ):
            raise ValueError("A delegation authority needs a frozenset of capability labels")


class WorkflowDelegationProposal(BaseModel):
    """What a Lead proposes: plan-authored task specs and nothing that carries authority.

    There is deliberately no field for owner, depth, parent, permissions or authorization;
    `extra="forbid"` rejects any such field. Every value here is untrusted input.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tasks: tuple[WorkflowTaskSpec, ...] = Field(min_length=1)


class WorkflowAdmissionDecision(BaseModel):
    """A preliminary admission answer for a workflow proposal. `code` is stable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: AdmissionOutcome
    code: str = Field(pattern=r"^[a-z_]{1,64}$")
    reason: str = Field(min_length=1, max_length=300)
    # Host-derived (parent depth plus one); only set when admitted.
    child_depth: int | None = Field(default=None, ge=1)
    # Logical id of the proposed task the outcome is about, when it is about one.
    subject: str | None = None
    # Always true: not a reservation and not the final authorization (ADR 0015, 4.2).
    preliminary: Literal[True] = True
    reserves_nothing: Literal[True] = True

    @property
    def admitted(self) -> bool:
        return self.outcome is AdmissionOutcome.ADMITTED


@dataclass(frozen=True, slots=True)
class DelegationCandidate:
    """What a host delegation source hands the controller: a parent id and untrusted task specs.

    There is no field for the run, the cycle, a depth, an owner, a workspace, permissions or an
    admission result: the controller owns the first three and derives the rest. `parent_task_id`
    must be one of the boundary's eligible parents or the controller refuses the candidate.
    """

    parent_task_id: str
    proposal: WorkflowDelegationProposal


class WorkflowDelegationSource(Protocol):
    """A host-controlled source of delegation candidates, consulted at the cycle boundary.

    It is called by the controller with a `DELEGATION_REQUESTED` request, never by a model or a
    task. It may return `None`. Its answer is untrusted input and grants nothing; durable,
    idempotent proposal storage is a later work package (ADR 0015, PR-C2).
    """

    async def propose(self, request: WorkflowRevisionRequest) -> DelegationCandidate | None: ...


_MAX_ROUNDS = 5


@dataclass(frozen=True, slots=True)
class WorkflowDelegation:
    """Opt-in delegation configuration for `WorkflowController` (disabled when absent).

    `max_rounds` bounds how many delegation cycles one execution may grant; further candidates
    block the run with an explicit reason instead of running or being ignored.
    """

    source: WorkflowDelegationSource
    admission: WorkflowDelegationAdmission
    max_rounds: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.max_rounds <= _MAX_ROUNDS:
            raise ValueError(f"max_rounds must be between 1 and {_MAX_ROUNDS}")


class WorkflowDelegationAdmission:
    """Read-only, preliminary admission of a Lead's delegation proposal."""

    def __init__(
        self,
        *,
        admission: DelegationAdmission,
        runs: DurableRunManager,
        tasks: TaskRunQuery,
        compiler: WorkflowCompiler,
        delegators: Mapping[str, DelegationAuthority],
        expected_workspace: WorkspaceBinding | None = None,
        parent_statuses: frozenset[TaskStatus] = frozenset({TaskStatus.RUNNING}),
        max_scan_tasks: int = 500,
    ) -> None:
        # `delegators` has no default: without the host-owned authority source nobody may
        # delegate. An empty mapping is allowed and refuses every parent.
        for worker_id, authority in delegators.items():
            if not isinstance(authority, DelegationAuthority) or authority.worker_id != worker_id:
                raise ValueError("Each delegator must be keyed by its own worker id")
            known = worker_id in compiler.worker_allowlist
            if known:
                try:
                    compiler.workers.get(worker_id)
                except WorkerNotFoundError:
                    known = False
            if not known:
                raise ValueError("A delegator must be an allowlisted, registered worker")
        if not 1 <= max_scan_tasks <= _MAX_SCAN:
            raise ValueError(f"max_scan_tasks must be between 1 and {_MAX_SCAN}")
        if not parent_statuses:
            raise ValueError("parent_statuses must not be empty")
        if not isinstance(tasks, TaskRunQuery):
            # Never fall back to the global listing: it cannot prove run ownership.
            raise TypeError("tasks must implement the TaskRunQuery capability")
        self.admission = admission
        self.runs = runs
        self.tasks = tasks
        self.compiler = compiler
        self.delegators: Mapping[str, DelegationAuthority] = MappingProxyType(dict(delegators))
        self.expected_workspace = expected_workspace
        self.parent_statuses = parent_statuses
        self.max_scan_tasks = max_scan_tasks

    async def evaluate(
        self,
        *,
        run_id: str,
        identity: RuntimeIdentity,
        parent_task_id: str,
        proposal: WorkflowDelegationProposal,
    ) -> WorkflowAdmissionDecision:
        """Whether `proposal` would be admitted now; a storage or policy failure never raises."""

        failure: str | None = None
        try:
            return await self._evaluate(
                run_id=run_id,
                identity=identity,
                parent_task_id=parent_task_id,
                proposal=proposal,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - fail closed; the message is never used
            failure = type(error).__name__
        _log_failure("state_unavailable", failure, run_id)
        return _deny("state_unavailable")

    async def _evaluate(
        self,
        *,
        run_id: str,
        identity: RuntimeIdentity,
        parent_task_id: str,
        proposal: WorkflowDelegationProposal,
    ) -> WorkflowAdmissionDecision:
        if (
            identity.tenant_id is None
            or identity.user_id is None
            or not run_id
            or not parent_task_id
        ):
            return _deny("invalid_trusted_state")
        run = await self.runs.get(run_id=run_id, identity=identity)
        if run is None or run.id != run_id or run.session_id != identity.session_id:
            return _deny("run_not_found")
        if run.status is not RuntimeRunStatus.RUNNING:
            return _deny("run_not_active")
        if self.expected_workspace is not None and run.workspace_binding != self.expected_workspace:
            return _deny("workspace_mismatch")
        if len(proposal.tasks) > self.compiler.limits.max_tasks_per_plan:
            return _deny("plan_too_large")

        scan_error: RunTaskScanError | None = None
        try:
            scan = await scan_run_tasks(self.tasks, run, max_tasks=self.max_scan_tasks)
        except RunTaskScanError as error:
            scan_error = error
        if scan_error is not None:
            _log_failure(scan_error.code, scan_error.error_type or "TaskRunPage", run.id)
            return _deny("state_unavailable")
        if scan.truncated or scan.unreadable > 0:
            # Counters are only authoritative over a complete inventory: unreadable rows and rows
            # dropped for not belonging to the run could be exactly the ones that use the quota.
            return _deny("state_incomplete")

        parent = next((task for task in scan.tasks if task.id == parent_task_id), None)
        if parent is None:
            return _deny("parent_not_found")
        if parent.status not in self.parent_statuses:
            return _deny("parent_not_active")
        authority = self.delegators.get(parent.agent_id)
        if authority is None:
            # Being a running, attested task does not confer the right to delegate.
            return _deny("delegator_not_authorized")
        if not self._holds_role(parent.agent_id, authority):
            return _deny("delegator_role_mismatch")
        active, in_run = _caller_delegations(scan.tasks, parent)

        denied = self._check_proposal(proposal)
        if denied is not None:
            return denied
        denied = self._dry_run(proposal, run, identity, scan.tasks)
        if denied is not None:
            return denied
        return await self._evaluate_tasks(
            proposal, run, identity, parent, authority, active=active, in_run=in_run
        )

    def _holds_role(self, worker_id: str, authority: DelegationAuthority) -> bool:
        """Whether the registered worker is the one the authority was granted to.

        A worker that declares a `role` (an `AgentRuntimeWorker`) must declare the authorized one;
        a worker without a role cannot be cross-checked and is trusted to its registered id. A
        worker that is no longer registered holds nothing.
        """

        try:
            worker = self.compiler.workers.get(worker_id)
        except WorkerNotFoundError:
            return False
        role = getattr(worker, "role", None)
        return role is None or (isinstance(role, str) and role == authority.role)

    def _check_proposal(
        self, proposal: WorkflowDelegationProposal
    ) -> WorkflowAdmissionDecision | None:
        """Cheap, deterministic checks against the compiler's host-owned allowlists."""

        compiler = self.compiler
        for spec in proposal.tasks:
            worker: object | None = None
            if spec.worker_id in compiler.worker_allowlist:
                try:
                    worker = compiler.workers.get(spec.worker_id)
                except WorkerNotFoundError:
                    worker = None
            if worker is None:
                return _deny("worker_not_allowed", subject=spec.id)
            if not spec.capabilities <= compiler.effective_capabilities:
                return _deny("capability_unavailable", subject=spec.id)
            # An `AgentRuntimeWorker` refuses a task whose capabilities exceed its own
            # `allowed_capabilities`, but only when it executes. Admit nothing that is doomed.
            declared = getattr(worker, "allowed_capabilities", None)
            if isinstance(declared, frozenset) and not spec.capabilities <= declared:
                return _deny("worker_capability_mismatch", subject=spec.id)
            if not set(spec.resource_keys) <= compiler.resource_allowlist:
                return _deny("resource_not_allowed", subject=spec.id)
        return None

    def _dry_run(
        self,
        proposal: WorkflowDelegationProposal,
        run: RuntimeRunRecord,
        identity: RuntimeIdentity,
        existing: tuple[AgentTask, ...],
    ) -> WorkflowAdmissionDecision | None:
        """Structural validation by the compiler (pure: it persists and schedules nothing)."""

        cycle = 1 + max((_cycle(task) for task in existing), default=0)
        try:
            self.compiler.compile(
                WorkflowPlan(objective=_DRY_RUN_OBJECTIVE, tasks=proposal.tasks),
                run=run,
                identity=identity,
                cycle=cycle,
            )
        except WorkflowPlanError:
            return _deny("plan_invalid")
        return None

    async def _evaluate_tasks(
        self,
        proposal: WorkflowDelegationProposal,
        run: RuntimeRunRecord,
        identity: RuntimeIdentity,
        parent: AgentTask,
        authority: DelegationAuthority,
        *,
        active: int,
        in_run: int,
    ) -> WorkflowAdmissionDecision:
        """Central admission per task; each task counts the ones before it in the batch.

        The caller's capabilities, for the shared checks and for the central policy, are the host
        authority's ceiling, never the parent's recorded labels. Those labels only narrow, and only
        for a delegated parent (depth of at least one), whose labels an earlier admission set.
        """

        narrowing = parent.capabilities if parent.delegation_depth >= 1 else None
        pending: WorkflowAdmissionDecision | None = None
        for position, spec in enumerate(proposal.tasks):
            decision = await self.admission.evaluate(
                DelegationClaim(
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                    session_id=run.session_id,
                    agent_id=parent.agent_id,
                    delegate_id=spec.worker_id,
                    parent_depth=parent.delegation_depth,
                    active_delegations=active + position,
                    delegations_in_run=in_run + position,
                    caller_capabilities=authority.capabilities,
                    requested_capabilities=spec.capabilities,
                    parent_capabilities=narrowing,
                    resource_keys=spec.resource_keys,
                    resource_allowlist=self.compiler.resource_allowlist,
                    conversation_id=run.conversation_id,
                    run_id=run.id,
                    trace_id=identity.trace_id or f"trace_{run.id}",
                    parent_id=parent.id,
                    caller_role=authority.role,
                    kind="workflow",
                )
            )
            if decision.outcome is AdmissionOutcome.DENIED:
                return _from_core(decision, subject=spec.id)
            if decision.outcome is AdmissionOutcome.APPROVAL_REQUIRED and pending is None:
                pending = _from_core(decision, subject=spec.id)
        if pending is not None:
            return pending  # a later denial would already have returned above
        return WorkflowAdmissionDecision(
            outcome=AdmissionOutcome.ADMITTED,
            code="admitted",
            reason="The proposal is admitted by the central policy at this moment",
            child_depth=parent.delegation_depth + 1,
        )


def _caller_delegations(tasks: tuple[AgentTask, ...], parent: AgentTask) -> tuple[int, int]:
    """Active and total delegated tasks of the caller's agent in this run.

    A delegated task is one with a depth of at least one. It counts for the caller unless its own
    parent task is known to belong to another agent; an unresolvable parent counts (fail closed).
    """

    by_id = {task.id: task for task in tasks}
    counted = []
    for task in tasks:
        if task.delegation_depth < 1:
            continue
        origin = by_id.get(task.parent_id) if task.parent_id else None
        if origin is None or origin.agent_id == parent.agent_id:
            counted.append(task)
    return sum(1 for task in counted if not task.terminal), len(counted)


def _cycle(task: AgentTask) -> int:
    value = task.metadata.get("workflow_cycle")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _deny(code: str, *, subject: str | None = None) -> WorkflowAdmissionDecision:
    return WorkflowAdmissionDecision(
        outcome=AdmissionOutcome.DENIED, code=code, reason=_REASONS[code], subject=subject
    )


def _from_core(decision: AdmissionDecision, *, subject: str) -> WorkflowAdmissionDecision:
    return WorkflowAdmissionDecision(
        outcome=decision.outcome,
        code=decision.code,
        reason=decision.reason,
        child_depth=decision.child_depth,
        subject=subject,
    )


def _log_failure(code: str, error_type: str | None, run_id: str) -> None:
    """Sanitized observability: a stable code, the exception class name and the run id only."""

    logger.warning(
        "workflow delegation admission failure code=%s error_type=%s run=%s",
        code,
        error_type or "Exception",
        run_id,
    )
