from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol
from uuid import uuid4

from kaiwen_agent.coordination import DistributedLease, LeaseStore
from kaiwen_agent.exceptions import ActionLookupIncompleteError, RunOwnershipError
from kaiwen_agent.policy import AdmissionOutcome
from kaiwen_agent.quality import (
    CompletionGate,
    CompletionGateResult,
    CompletionGateStatus,
    RepairDecision,
    RepairTracker,
    VerificationContext,
)
from kaiwen_agent.runtime import (
    DurableRunManager,
    RuntimeIdentity,
    RuntimeRunConflictError,
    RuntimeRunRecord,
    RuntimeRunStatus,
    WorkspaceBinding,
)
from kaiwen_agent.standards import StandardResolution
from kaiwen_agent.tasks import AgentTask, TaskGraph, TaskScheduler, TaskStatus
from kaiwen_agent.tools.durability import ActionIntentStatus, ActionIntentStore
from kaiwen_agent.types import utc_now
from kaiwen_agent.workflow.compiler import DelegationBinding, WorkflowCompiler, WorkflowPlanError
from kaiwen_agent.workflow.delegation import DelegationCandidate, WorkflowDelegation
from kaiwen_agent.workflow.models import (
    DelegationBoundary,
    DelegationParent,
    WorkflowLimits,
    WorkflowOutcome,
    WorkflowOutcomeStatus,
    WorkflowPlan,
    WorkflowRevisionReason,
    WorkflowRevisionRequest,
    WorkflowTaskFailure,
)

SchedulerFactory = Callable[[TaskGraph], TaskScheduler]

logger = logging.getLogger(__name__)

_UNCERTAIN_PAGE = 1_000


class WorkflowRevisionPlanner(Protocol):
    async def revise(self, request: WorkflowRevisionRequest) -> WorkflowPlan | None: ...


class ProtectedInputFingerprintProvider(Protocol):
    """Trusted, host-owned source of the current state fingerprint for the gate.

    It is called by the controller, never by a model or task. An exception means the
    state cannot be established, and completion then fails closed.
    """

    async def current(self, *, run: RuntimeRunRecord, identity: RuntimeIdentity) -> str: ...


class _Ownership:
    lost = False
    lease: DistributedLease | None = None


@dataclass(frozen=True, slots=True)
class _DelegationStep:
    """The next cycle of a delegation the controller itself just authorized."""

    plan: WorkflowPlan
    binding: DelegationBinding


class _RunWriter:
    """Run-state writes for one execution, conditional on the owner's fencing token.

    Every write also requires the stored status to be unchanged since it was read, so a
    stale executor whose lease was taken over cannot overwrite the new owner's state; it
    stops with `RunOwnershipError` instead.
    """

    def __init__(self, runs: DurableRunManager, fence: int | None) -> None:
        self.runs = runs
        self.fence = fence

    async def start(
        self, *, run_id: str, identity: RuntimeIdentity, owner_lease_key: str | None
    ) -> RuntimeRunRecord:
        return await self._guard(
            self.runs.start(
                run_id=run_id,
                identity=identity,
                owner_lease_key=owner_lease_key,
                owner_fence=self.fence,
            )
        )

    async def fail(
        self, *, run_id: str, identity: RuntimeIdentity, reason: str
    ) -> RuntimeRunRecord:
        return await self._guard(
            self.runs.fail(run_id=run_id, identity=identity, reason=reason, owner_fence=self.fence)
        )

    async def complete(self, *, run_id: str, identity: RuntimeIdentity) -> RuntimeRunRecord:
        return await self._guard(
            self.runs.complete(run_id=run_id, identity=identity, owner_fence=self.fence)
        )

    async def block(
        self, *, run_id: str, identity: RuntimeIdentity, reason: str
    ) -> RuntimeRunRecord:
        return await self._guard(
            self.runs.block(run_id=run_id, identity=identity, reason=reason, owner_fence=self.fence)
        )

    async def cancel(self, *, run_id: str, identity: RuntimeIdentity) -> RuntimeRunRecord:
        return await self._guard(
            self.runs.cancel(run_id=run_id, identity=identity, owner_fence=self.fence)
        )

    @staticmethod
    async def _guard(write: Any) -> RuntimeRunRecord:
        try:
            result: RuntimeRunRecord = await write
            return result
        except RuntimeRunConflictError as error:
            raise RunOwnershipError(
                "The run changed under another owner; this executor stopped without writing"
            ) from error


class WorkflowController:
    """Finite control plane over TaskScheduler and CompletionGate."""

    def __init__(
        self,
        *,
        runs: DurableRunManager,
        compiler: WorkflowCompiler,
        scheduler_factory: SchedulerFactory,
        completion_gate: CompletionGate,
        repair_tracker: RepairTracker | None = None,
        revision_planner: WorkflowRevisionPlanner | None = None,
        limits: WorkflowLimits | None = None,
        action_intents: ActionIntentStore | None = None,
        leases: LeaseStore | None = None,
        lease_owner_id: str | None = None,
        lease_ttl_seconds: float = 30.0,
        fingerprint_provider: ProtectedInputFingerprintProvider | None = None,
        workspace_binding: WorkspaceBinding | None = None,
        block_unassociated_actions: bool = False,
        delegation: WorkflowDelegation | None = None,
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")
        if delegation is not None:
            # Delegation is opt-in and fails closed at configuration, not at run time.
            if leases is None:
                raise ValueError(
                    "Delegation needs a lease store: only the run's lease holder may create "
                    "its tasks, which is what keeps run-scoped quotas from being raced"
                )
            admission = delegation.admission
            if admission.compiler is not compiler:
                raise ValueError("Delegation admission must use this controller's compiler")
            if admission.parent_statuses != frozenset({TaskStatus.COMPLETED}):
                raise ValueError(
                    "Delegation admission must accept only completed parents: the controller "
                    "delegates at the boundary after a cycle, never from a running task"
                )
            if admission.expected_workspace != workspace_binding:
                raise ValueError(
                    "Delegation admission must expect the controller's workspace binding"
                )

        self.runs = runs
        self.compiler = compiler
        self.scheduler_factory = scheduler_factory
        self.completion_gate = completion_gate
        self.repair_limits = (repair_tracker or RepairTracker()).limits
        self.revision_planner = revision_planner
        self.limits = limits or compiler.limits
        # Optional: lets cancellation distinguish a safe stop from an unknown
        # external outcome. Without it, cancellation is recorded as CANCELLED.
        self.action_intents = action_intents
        # Optional run ownership: the controller holds a heartbeat lease while it
        # executes so recovery can tell a live owner from a dead one.
        self.leases = leases
        self.lease_owner_id = lease_owner_id or f"controller-{uuid4().hex}"
        self.lease_ttl_seconds = lease_ttl_seconds
        # Optional. Without it the fingerprint passed to `execute` is used for every
        # gate evaluation (the original behavior). With it, each evaluation uses a
        # freshly obtained value and the passed fingerprint only names the input.
        self.fingerprint_provider = fingerprint_provider
        # Optional. When set, a run must carry exactly this durable workspace binding.
        self.workspace_binding = workspace_binding
        # Optional, needs `action_intents`. Actions of workflow workers written before they
        # were owned by the durable run carry a per-invocation id, so no run can be proven to
        # own them. When set, a run owner with such an unresolved (executing or uncertain)
        # action cannot start work until an operator resolves it. See docs/persistence.md.
        self.block_unassociated_actions = block_unassociated_actions
        # Optional and off by default. Without it the controller behaves exactly as before.
        self.delegation = delegation

    async def execute(
        self,
        *,
        run: RuntimeRunRecord,
        identity: RuntimeIdentity,
        plan: WorkflowPlan,
        standards: StandardResolution,
        protected_input_fingerprint: str,
    ) -> WorkflowOutcome:
        lease: DistributedLease | None = None
        heartbeat: asyncio.Task[None] | None = None
        ownership = _Ownership()
        writer = _RunWriter(self.runs, None)
        if self.leases is not None:
            lease = await self.leases.acquire(
                key=f"runtime-run:{run.id}",
                owner_id=self.lease_owner_id,
                ttl_seconds=self.lease_ttl_seconds,
            )
            if lease is None:
                raise RunOwnershipError("Workflow run is owned by another executor")
            owner_task = asyncio.current_task()
            assert owner_task is not None
            writer = _RunWriter(self.runs, lease.fencing_token)
            ownership.lease = lease
            heartbeat = asyncio.create_task(self._heartbeat(lease, owner_task, ownership))
        try:
            return await self._execute(
                run=run,
                identity=identity,
                plan=plan,
                standards=standards,
                protected_input_fingerprint=protected_input_fingerprint,
                owner_lease_key=lease.key if lease is not None else None,
                writer=writer,
                ownership=ownership,
            )
        except asyncio.CancelledError:
            if ownership.lost:
                # Another executor may now own the run; do not write state on its behalf.
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                raise RunOwnershipError(
                    "Run ownership was lost; execution stopped without recording state"
                ) from None
            await asyncio.shield(
                self._record_cancellation(run=run, identity=identity, writer=writer)
            )
            raise
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            if lease is not None and self.leases is not None and not ownership.lost:
                with suppress(Exception):
                    await asyncio.shield(self.leases.release(lease))

    async def _heartbeat(
        self, lease: DistributedLease, owner: asyncio.Task[object], ownership: _Ownership
    ) -> None:
        assert self.leases is not None
        current = lease
        while True:
            await asyncio.sleep(self.lease_ttl_seconds / 3)
            try:
                renewed = await self.leases.renew(current, ttl_seconds=self.lease_ttl_seconds)
            except Exception:  # noqa: BLE001 - a transient store error is retried until expiry
                renewed = None if utc_now() >= current.expires_at else current
            if renewed is None:
                ownership.lost = True
                owner.cancel()
                return
            current = renewed

    async def _record_cancellation(
        self, *, run: RuntimeRunRecord, identity: RuntimeIdentity, writer: _RunWriter
    ) -> None:
        # Cancellation is only recorded as safe when the lookup proves that no action is
        # in flight. An incomplete or failed lookup is not proof, so the run is blocked.
        unverified: str | None = None
        uncertain: list[str] = []
        try:
            uncertain = await self._uncertain_actions(run)
        except ActionLookupIncompleteError:
            unverified = "lookup_incomplete"
        except Exception:  # noqa: BLE001 - any lookup failure must fail closed
            logger.warning("Could not look up in-flight actions for run %s", run.id, exc_info=True)
            unverified = "unverified"
        try:
            if uncertain or unverified is not None:
                await writer.block(
                    run_id=run.id,
                    identity=identity,
                    reason=f"uncertain_action:{uncertain[0] if uncertain else unverified}",
                )
            else:
                await writer.cancel(run_id=run.id, identity=identity)
        except (KeyError, ValueError, RunOwnershipError):
            logger.warning("Could not record cancellation for run %s", run.id, exc_info=True)

    async def _uncertain_actions(self, run: RuntimeRunRecord) -> list[str]:
        store = self.action_intents
        if store is None:
            return []
        statuses = frozenset({ActionIntentStatus.EXECUTING, ActionIntentStatus.UNCERTAIN})
        scoped = getattr(store, "list_run_action_intents", None)
        if scoped is not None:
            scoped_intents = await scoped(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                runtime_run_id=run.id,
                statuses=statuses,
            )
            return sorted(intent.id for intent in scoped_intents)
        page = await store.list_action_intents(statuses=statuses, limit=_UNCERTAIN_PAGE)
        if len(page) >= _UNCERTAIN_PAGE:
            # A full page may hide this run's intents behind unrelated ones.
            raise ActionLookupIncompleteError("In-flight action listing may be truncated")
        return sorted(
            intent.id
            for intent in page
            if intent.runtime_run_id == run.id
            and intent.tenant_id == run.tenant_id
            and intent.user_id == run.user_id
        )

    async def _unresolved_run_actions(self, run: RuntimeRunRecord) -> list[str]:
        """Executing or uncertain actions owned by this run; fails closed on lookup errors.

        At this point the run is not RUNNING (a RUNNING run is refused above) and, with a
        lease store, this controller holds the exclusive run lease, so no live owner can have
        an action in flight for it. An EXECUTING action is therefore the orphan of a dead
        owner whose outcome is unknown, exactly like an UNCERTAIN one. The controller does not
        run recovery itself, so it must not assume `reconcile` ran first. Without a configured
        action store the controller cannot know and does nothing.
        """

        if self.action_intents is None:
            return []
        try:
            return await self._uncertain_actions(run)
        except ActionLookupIncompleteError:
            return ["lookup_incomplete"]
        except Exception:  # noqa: BLE001 - any lookup failure must fail closed
            logger.warning("Could not look up actions of run %s", run.id, exc_info=True)
            return ["unverified"]

    async def _unassociated_actions(
        self, run: RuntimeRunRecord, identity: RuntimeIdentity
    ) -> list[str]:
        """Unresolved actions of this owner that no durable run is known to own.

        Fails closed: no action store, an incomplete listing or any lookup error counts as
        unresolved, because absence of proof is not proof of absence.
        """

        store = self.action_intents
        if store is None:
            return ["no_action_store"]
        try:
            page = await store.list_action_intents(
                statuses=frozenset({ActionIntentStatus.EXECUTING, ActionIntentStatus.UNCERTAIN}),
                limit=_UNCERTAIN_PAGE,
            )
            if len(page) >= _UNCERTAIN_PAGE:
                return ["lookup_incomplete"]
            unresolved: list[str] = []
            for intent in page:
                if (intent.tenant_id, intent.user_id) != (run.tenant_id, run.user_id):
                    continue
                if intent.runtime_run_id == run.id:
                    continue
                owner = await self.runs.get(run_id=intent.runtime_run_id, identity=identity)
                if owner is None:
                    unresolved.append(intent.id)
            return sorted(unresolved)
        except Exception:  # noqa: BLE001 - any lookup failure must fail closed
            logger.warning("Could not look up unassociated actions", exc_info=True)
            return ["unverified"]

    async def _execute(
        self,
        *,
        run: RuntimeRunRecord,
        identity: RuntimeIdentity,
        plan: WorkflowPlan,
        standards: StandardResolution,
        protected_input_fingerprint: str,
        owner_lease_key: str | None = None,
        writer: _RunWriter,
        ownership: _Ownership,
    ) -> WorkflowOutcome:
        if run.status not in {
            RuntimeRunStatus.CREATED,
            RuntimeRunStatus.PAUSED,
            RuntimeRunStatus.BLOCKED,
        }:
            raise ValueError(f"Workflow run cannot start from {run.status}")
        current_run = await writer.start(
            run_id=run.id, identity=identity, owner_lease_key=owner_lease_key
        )
        if self.workspace_binding is not None and (
            current_run.workspace_binding != self.workspace_binding
        ):
            # Compared with the durable record just read, never with the caller's copy.
            return await self._blocked(
                writer=writer,
                run=run,
                identity=identity,
                cycle=1,
                tasks=[],
                quality=None,
                reason="workspace_binding_mismatch",
            )
        # A run that is restarted (blocked, paused) must not start new work while one of its
        # own actions has an unknown outcome: a different tool-call id is not proof of a
        # different external operation. Only an explicit operator resolution lifts this.
        same_run = await self._unresolved_run_actions(run)
        if same_run:
            return await self._blocked(
                writer=writer,
                run=run,
                identity=identity,
                cycle=1,
                tasks=[],
                quality=None,
                reason=f"uncertain_action:{same_run[0]}",
            )
        if self.block_unassociated_actions:
            unresolved = await self._unassociated_actions(run, identity)
            if unresolved:
                return await self._blocked(
                    writer=writer,
                    run=run,
                    identity=identity,
                    cycle=1,
                    tasks=[],
                    quality=None,
                    reason=f"unassociated_action:{unresolved[0]}",
                )
        current_plan = plan
        all_tasks: list[AgentTask] = []
        # Delegation state, local to this execution: nothing here survives a restart, so a
        # replay re-derives and re-authorizes everything instead of trusting an earlier answer.
        pending_binding: DelegationBinding | None = None
        delegation_rounds = 0
        latest_quality: CompletionGateResult | None = None
        repair_tracker = RepairTracker(self.repair_limits)
        for cycle in range(1, self.limits.max_cycles + 1):
            binding, pending_binding = pending_binding, None
            try:
                graph = self.compiler.compile(
                    current_plan,
                    run=current_run,
                    identity=identity,
                    cycle=cycle,
                    delegation=binding,
                )
            except WorkflowPlanError as error:
                failed = await writer.fail(
                    run_id=run.id,
                    identity=identity,
                    reason="invalid_plan",
                )
                return WorkflowOutcome(
                    status=WorkflowOutcomeStatus.FAILED,
                    run=failed,
                    cycles=cycle,
                    tasks=tuple(all_tasks),
                    reason=str(error),
                )
            if binding is not None:
                # Delegated work: ask the lease store again right before it is scheduled.
                await self._verify_ownership(
                    run=run, identity=identity, writer=writer, ownership=ownership
                )
            try:
                tasks = await self.scheduler_factory(graph).run()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failed = await writer.fail(
                    run_id=run.id,
                    identity=identity,
                    reason="scheduler_failed",
                )
                return WorkflowOutcome(
                    status=WorkflowOutcomeStatus.FAILED,
                    run=failed,
                    cycles=cycle,
                    tasks=tuple(all_tasks),
                    reason=f"scheduler_failed:{type(error).__name__}",
                )
            all_tasks.extend(tasks)
            failures = tuple(
                WorkflowTaskFailure(
                    task_id=task.id,
                    logical_task_id=str(task.metadata.get("logical_task_id", task.id)),
                    error_code=task.error_code,
                )
                for task in tasks
                if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}
            )
            if failures:
                try:
                    revised = await self._revise(
                        WorkflowRevisionRequest(
                            run_id=run.id,
                            cycle=cycle,
                            reason=WorkflowRevisionReason.EXECUTION_FAILED,
                            prior_plan=current_plan,
                            task_failures=failures,
                        ),
                        allow=(cycle < self.limits.max_cycles and repair_tracker.request_replan()),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    return await self._revision_failure(
                        writer=writer,
                        run=run,
                        identity=identity,
                        cycle=cycle,
                        tasks=all_tasks,
                        error=error,
                    )
                if revised is not None and cycle < self.limits.max_cycles:
                    current_plan = revised
                    continue
                failed = await writer.fail(
                    run_id=run.id,
                    identity=identity,
                    reason="task_execution_failed",
                )
                return WorkflowOutcome(
                    status=WorkflowOutcomeStatus.FAILED,
                    run=failed,
                    cycles=cycle,
                    tasks=tuple(all_tasks),
                    reason="task_execution_failed",
                )

            if self.delegation is not None:
                step = await self._delegation_boundary(
                    run=run,
                    identity=identity,
                    writer=writer,
                    ownership=ownership,
                    cycle=cycle,
                    plan=current_plan,
                    tasks=tasks,
                    all_tasks=all_tasks,
                    rounds=delegation_rounds,
                )
                if isinstance(step, WorkflowOutcome):
                    return step
                if step is not None:
                    current_plan = step.plan
                    pending_binding = step.binding
                    delegation_rounds += 1
                    continue

            gate_fingerprint = protected_input_fingerprint
            if self.fingerprint_provider is not None:
                try:
                    gate_fingerprint = await self._fresh_fingerprint(run, identity)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Current protected input unavailable", exc_info=True)
                    return await self._blocked(
                        writer=writer,
                        run=run,
                        identity=identity,
                        cycle=cycle,
                        tasks=all_tasks,
                        quality=None,
                        reason="protected_input_unavailable",
                    )
            try:
                latest_quality = await self.completion_gate.evaluate(
                    standards,
                    context=VerificationContext(
                        tenant_id=run.tenant_id,
                        user_id=run.user_id,
                        session_id=run.session_id,
                        trace_id=identity.trace_id or f"trace_{run.id}",
                        run_id=run.id,
                        protected_input_fingerprint=gate_fingerprint,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failed = await writer.fail(
                    run_id=run.id,
                    identity=identity,
                    reason="completion_gate_failed",
                )
                return WorkflowOutcome(
                    status=WorkflowOutcomeStatus.FAILED,
                    run=failed,
                    cycles=cycle,
                    tasks=tuple(all_tasks),
                    reason=f"completion_gate_failed:{type(error).__name__}",
                )
            if latest_quality.status == CompletionGateStatus.PASS:
                if self.fingerprint_provider is not None:
                    # Bracket the evaluation: a different state afterwards means the
                    # verdict may describe state that no longer exists. This detects a
                    # concurrent writer; it does not make the workspace atomic.
                    try:
                        confirmed = await self._fresh_fingerprint(run, identity)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("Protected input unavailable after gate", exc_info=True)
                        confirmed = ""
                    if confirmed != gate_fingerprint:
                        return await self._blocked(
                            writer=writer,
                            run=run,
                            identity=identity,
                            cycle=cycle,
                            tasks=all_tasks,
                            quality=None,
                            reason="workspace_changed_during_verification"
                            if confirmed
                            else "protected_input_unavailable",
                        )
                completed = await writer.complete(run_id=run.id, identity=identity)
                return WorkflowOutcome(
                    status=WorkflowOutcomeStatus.COMPLETED,
                    run=completed,
                    cycles=cycle,
                    tasks=tuple(all_tasks),
                    quality_result=latest_quality,
                    verified_input_fingerprint=gate_fingerprint,
                )
            if latest_quality.status in {
                CompletionGateStatus.APPROVAL_REQUIRED,
                CompletionGateStatus.CONFIGURATION_ERROR,
            }:
                return await self._blocked(
                    writer=writer,
                    run=run,
                    identity=identity,
                    cycle=cycle,
                    tasks=all_tasks,
                    quality=latest_quality,
                    reason=latest_quality.status.value,
                )
            repair_allowed = self._record_quality_failures(
                latest_quality,
                repair_tracker=repair_tracker,
            )
            try:
                revised = await self._revise(
                    WorkflowRevisionRequest(
                        run_id=run.id,
                        cycle=cycle,
                        reason=WorkflowRevisionReason.QUALITY_BLOCKED,
                        prior_plan=current_plan,
                        quality_result=latest_quality,
                    ),
                    allow=cycle < self.limits.max_cycles and repair_allowed,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                return await self._revision_failure(
                    writer=writer,
                    run=run,
                    identity=identity,
                    cycle=cycle,
                    tasks=all_tasks,
                    error=error,
                )
            if revised is not None and cycle < self.limits.max_cycles:
                current_plan = revised
                continue
            return await self._blocked(
                writer=writer,
                run=run,
                identity=identity,
                cycle=cycle,
                tasks=all_tasks,
                quality=latest_quality,
                reason="quality_blocked",
            )

        return await self._blocked(
            writer=writer,
            run=run,
            identity=identity,
            cycle=self.limits.max_cycles,
            tasks=all_tasks,
            quality=latest_quality,
            reason="workflow_cycle_limit",
        )

    async def _delegation_boundary(
        self,
        *,
        run: RuntimeRunRecord,
        identity: RuntimeIdentity,
        writer: _RunWriter,
        ownership: _Ownership,
        cycle: int,
        plan: WorkflowPlan,
        tasks: tuple[AgentTask, ...],
        all_tasks: list[AgentTask],
        rounds: int,
    ) -> _DelegationStep | WorkflowOutcome | None:
        """The one place a delegation can become executable (ADR 0015, 4.2).

        Reached only after a cycle whose tasks all ended without failure and before the
        completion gate. Returns `None` when the source has no candidate (the gate then judges
        completion as before), the authorized next cycle, or a BLOCKED outcome with an explicit
        reason. A candidate is never ignored and never treated as completion: anything short of a
        fresh, explicit allow blocks the run.

        The authorization here is a fresh evaluation, not a stored answer, and it is only as
        atomic as the run lease: the controller holds the lease and re-verifies its fencing token
        immediately after the evaluation and before anything is compiled, so no other writer can
        create this run's tasks in between. It does not make external side effects fenced.
        """

        assert self.delegation is not None
        config = self.delegation
        parents = tuple(
            DelegationParent(
                task_id=task.id,
                logical_task_id=str(task.metadata.get("logical_task_id", task.id)),
                worker_id=task.agent_id,
                depth=task.delegation_depth,
            )
            for task in tasks
            if task.status is TaskStatus.COMPLETED
        )
        request = WorkflowRevisionRequest(
            run_id=run.id,
            cycle=cycle,
            reason=WorkflowRevisionReason.DELEGATION_REQUESTED,
            prior_plan=plan,
            delegation=DelegationBoundary(
                eligible_parents=parents, rounds_used=rounds, rounds_allowed=config.max_rounds
            ),
        )

        def blocked(reason: str) -> Coroutine[Any, Any, WorkflowOutcome]:
            return self._blocked(
                writer=writer,
                run=run,
                identity=identity,
                cycle=cycle,
                tasks=all_tasks,
                quality=None,
                reason=reason,
            )

        failure: str | None = None
        candidate: object = None
        try:
            candidate = await config.source.propose(request)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - fail closed; the message is never used
            failure = type(error).__name__
        if failure is not None:  # handled outside the handler so nothing is chained or logged
            logger.warning("delegation source failure error_type=%s run=%s", failure, run.id)
            return await blocked("delegation_source_failed")
        if candidate is None:
            return None
        if not isinstance(candidate, DelegationCandidate):
            return await blocked("delegation_invalid_candidate")
        if rounds >= config.max_rounds:
            return await blocked("delegation_round_limit")
        if cycle >= self.limits.max_cycles:
            return await blocked("delegation_cycle_limit")
        if candidate.parent_task_id not in {parent.task_id for parent in parents}:
            # Only a task that completed in the cycle that just ended may delegate. Task ids
            # carry their cycle, so a parent is eligible at exactly one boundary: a replayed
            # candidate cannot be admitted twice within an execution.
            return await blocked("delegation_parent_not_eligible")

        decision = await config.admission.evaluate(
            run_id=run.id,
            identity=identity,
            parent_task_id=candidate.parent_task_id,
            proposal=candidate.proposal,
        )
        if decision.outcome is AdmissionOutcome.APPROVAL_REQUIRED:
            return await blocked(f"delegation_approval_required:{decision.code}")
        if not decision.admitted:
            return await blocked(f"delegation_denied:{decision.code}")
        if decision.child_depth is None:
            return await blocked("delegation_invalid_decision")
        # Still the owner? This is the last check before the delegated plan is compiled.
        await self._verify_ownership(run=run, identity=identity, writer=writer, ownership=ownership)
        return _DelegationStep(
            # The plan is the admitted task tuple itself: no planner can substitute or add to it.
            plan=WorkflowPlan(
                version=plan.version + 1, objective=plan.objective, tasks=candidate.proposal.tasks
            ),
            binding=DelegationBinding(
                parent_task_id=candidate.parent_task_id, child_depth=decision.child_depth
            ),
        )

    async def _verify_ownership(
        self,
        *,
        run: RuntimeRunRecord,
        identity: RuntimeIdentity,
        writer: _RunWriter,
        ownership: _Ownership,
    ) -> None:
        """Raise `RunOwnershipError` unless this executor still owns the running run.

        The run record's stored fencing token is not proof that the lease is held: the lease can
        expire or be replaced while the record still names the old token (the new owner has not
        written yet), and the heartbeat only notices at its next tick. The lease store is
        therefore asked directly: `renew` succeeds only for the current, unexpired holder. This
        is a check at one instant, not an atomic guarantee: the lease can still be lost after it
        and task writes are not fenced by the lease store.
        """

        if ownership.lost:
            raise RunOwnershipError("Run ownership was lost; delegation stopped")
        lease = ownership.lease
        if lease is not None and self.leases is not None:
            held = False
            try:
                held = (
                    await self.leases.renew(lease, ttl_seconds=self.lease_ttl_seconds)
                ) is not None
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - an unknown lease state is not ownership
                # The class name only: a lease backend's message can carry connection secrets.
                logger.warning(
                    "Run lease could not be verified (lease_store_unavailable: %s)",
                    type(error).__name__,
                )
            if not held:
                ownership.lost = True
                raise RunOwnershipError("The run lease is no longer held; delegation stopped")
        record = await self.runs.get(run_id=run.id, identity=identity)
        if (
            record is None
            or record.status is not RuntimeRunStatus.RUNNING
            or (writer.fence is not None and record.owner_fencing_token != writer.fence)
        ):
            raise RunOwnershipError("The run changed under another owner; delegation stopped")

    async def _fresh_fingerprint(self, run: RuntimeRunRecord, identity: RuntimeIdentity) -> str:
        assert self.fingerprint_provider is not None
        value = await self.fingerprint_provider.current(run=run, identity=identity)
        if not isinstance(value, str) or not value:
            raise ValueError("The fingerprint provider returned no fingerprint")
        return value

    async def _revise(
        self, request: WorkflowRevisionRequest, *, allow: bool
    ) -> WorkflowPlan | None:
        if not allow or self.revision_planner is None:
            return None
        return await self.revision_planner.revise(request)

    @staticmethod
    def _record_quality_failures(
        result: CompletionGateResult, *, repair_tracker: RepairTracker
    ) -> bool:
        decisions: list[RepairDecision] = []
        for verification in result.verifications:
            if verification.status.value == "pass":
                continue
            material = "\x1f".join(
                (
                    verification.requirement_id,
                    verification.status.value,
                    *verification.findings,
                )
            )
            decisions.append(
                repair_tracker.record_failure(
                    verification.requirement_id,
                    sha256(material.encode()).hexdigest(),
                )
            )
        return bool(decisions) and all(decision == RepairDecision.REPAIR for decision in decisions)

    async def _blocked(
        self,
        *,
        writer: _RunWriter,
        run: RuntimeRunRecord,
        identity: RuntimeIdentity,
        cycle: int,
        tasks: list[AgentTask],
        quality: CompletionGateResult | None,
        reason: str,
    ) -> WorkflowOutcome:
        blocked = await writer.block(run_id=run.id, identity=identity, reason=reason)
        return WorkflowOutcome(
            status=WorkflowOutcomeStatus.BLOCKED,
            run=blocked,
            cycles=cycle,
            tasks=tuple(tasks),
            quality_result=quality,
            reason=reason,
        )

    async def _revision_failure(
        self,
        *,
        writer: _RunWriter,
        run: RuntimeRunRecord,
        identity: RuntimeIdentity,
        cycle: int,
        tasks: list[AgentTask],
        error: Exception,
    ) -> WorkflowOutcome:
        failed = await writer.fail(
            run_id=run.id,
            identity=identity,
            reason="revision_planner_failed",
        )
        return WorkflowOutcome(
            status=WorkflowOutcomeStatus.FAILED,
            run=failed,
            cycles=cycle,
            tasks=tuple(tasks),
            reason=f"revision_planner_failed:{type(error).__name__}",
        )
