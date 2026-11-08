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
found in the run's attested listing, so it is a host record; its `delegation_depth`,
`capabilities` and `agent_id` are what the policy sees (parent depth is read from it and the
child's depth is parent plus one, computed by the host). `parent_task_id` must come from trusted
context, never from the proposal. The proposal holds only plan-authored task specs; it has no
field for owner, depth, permissions or authorization, and unknown fields are rejected.

Limits of the answer (ADR 0015, 4.2). The result is preliminary and point-in-time: nothing is
reserved, concurrent evaluations can all be admitted, and `WorkflowCompiler` alone is not a
final authorization because it neither evaluates the policy nor knows live delegation quotas.
The final, fresh authorization belongs to the controller integration (PR-C1). Per-run counters
here are per run: the job path counts a caller's active jobs across runs, this path cannot.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

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
from kaiwen_agent.workflow.models import WorkflowPlan, WorkflowTaskSpec
from kaiwen_agent.workflow.run_tasks import RunTaskScanError, scan_run_tasks

logger = logging.getLogger(__name__)

_MAX_SCAN = 1_000
_DRY_RUN_OBJECTIVE = "delegation admission dry run"

_REASONS = {
    "invalid_trusted_state": "Delegation facts are missing or malformed",
    "state_unavailable": "Trusted workflow state could not be read",
    "state_incomplete": "Trusted workflow state is too large to establish counters",
    "run_not_found": "The workflow run was not found for this owner",
    "run_not_active": "The workflow run is not running",
    "workspace_mismatch": "The workflow run is bound to a different workspace",
    "plan_too_large": "The proposal exceeds the task limit",
    "parent_not_found": "The parent task was not found in this run",
    "parent_not_active": "The parent task is not in a state that may delegate",
    "worker_not_allowed": "A proposed worker is not allowlisted",
    "capability_unavailable": "A proposed capability is not available to the host",
    "resource_not_allowed": "Requested resource keys are not allowlisted",
    "plan_invalid": "The proposed plan is not valid",
}


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


class WorkflowDelegationAdmission:
    """Read-only, preliminary admission of a Lead's delegation proposal."""

    def __init__(
        self,
        *,
        admission: DelegationAdmission,
        runs: DurableRunManager,
        tasks: TaskRunQuery,
        compiler: WorkflowCompiler,
        expected_workspace: WorkspaceBinding | None = None,
        parent_statuses: frozenset[TaskStatus] = frozenset({TaskStatus.RUNNING}),
        max_scan_tasks: int = 500,
    ) -> None:
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
        if scan.truncated:
            return _deny("state_incomplete")  # counters cannot be established: fail closed

        parent = next((task for task in scan.tasks if task.id == parent_task_id), None)
        if parent is None:
            return _deny("parent_not_found")
        if parent.status not in self.parent_statuses:
            return _deny("parent_not_active")
        active, in_run = _caller_delegations(scan.tasks, parent)

        denied = self._check_proposal(proposal)
        if denied is not None:
            return denied
        denied = self._dry_run(proposal, run, identity, scan.tasks)
        if denied is not None:
            return denied
        return await self._evaluate_tasks(
            proposal, run, identity, parent, active=active, in_run=in_run
        )

    def _check_proposal(
        self, proposal: WorkflowDelegationProposal
    ) -> WorkflowAdmissionDecision | None:
        """Cheap, deterministic checks against the compiler's host-owned allowlists."""

        compiler = self.compiler
        for spec in proposal.tasks:
            known = spec.worker_id in compiler.worker_allowlist
            if known:
                try:
                    compiler.workers.get(spec.worker_id)
                except WorkerNotFoundError:
                    known = False
            if not known:
                return _deny("worker_not_allowed", subject=spec.id)
            if not spec.capabilities <= compiler.effective_capabilities:
                return _deny("capability_unavailable", subject=spec.id)
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
        *,
        active: int,
        in_run: int,
    ) -> WorkflowAdmissionDecision:
        """Central admission per task; each task counts the ones before it in the batch."""

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
                    caller_capabilities=parent.capabilities,
                    requested_capabilities=spec.capabilities,
                    parent_capabilities=parent.capabilities,
                    resource_keys=spec.resource_keys,
                    resource_allowlist=self.compiler.resource_allowlist,
                    conversation_id=run.conversation_id,
                    run_id=run.id,
                    trace_id=identity.trace_id or f"trace_{run.id}",
                    parent_id=parent.id,
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
