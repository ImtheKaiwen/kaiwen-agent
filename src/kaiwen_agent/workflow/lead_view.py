"""Read-only Lead coordination view of one durable workflow run (ADR 0015, M6.3.3-A).

`LeadViewAssembler` builds a bounded `LeadView` from the existing authoritative stores: the
durable run record, the owner- and run-scoped task listing (`TaskRunQuery`), the artifact
store through `TaskContextAssembler`'s ownership and evidence-authentication rules, and, when
configured, owner-scoped approval retrieval.

It is a projection, not a coordinator. Construction performs no write, runs no tool, creates no
task, resolves no approval and mutates no action intent, and it takes no lease. Fields are
labelled by provenance:

- host-derived facts: run and task identity, status, structure, attempts, progress, status-derived
  blocker categories, pattern-checked error codes, and references the host verified;
- untrusted text: task names and summaries written by a plan or a model, sanitized and clipped.

A source that was not configured or could not be read is reported as unavailable, never as empty.
The view never reports whether a requirement passed: an authenticated evidence reference is
authentic, not proof of satisfaction, and the completion gate stays authoritative.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.context_engine import ContextBuildRequest, ContextItem, ContextKind
from kaiwen_agent.runtime import (
    DurableRunManager,
    RuntimeIdentity,
    RuntimeRunRecord,
    RuntimeRunStatus,
    WorkspaceBinding,
)
from kaiwen_agent.tasks import MAX_TASK_RUN_PAGE, AgentTask, TaskRunQuery, TaskStatus
from kaiwen_agent.tools.durability import (
    ActionIntentStatus,
    ActionIntentStore,
    ApprovalStatus,
    DurableApprovalStore,
)
from kaiwen_agent.workflow.task_context import TaskContextAssembler

logger = logging.getLogger(__name__)

MAX_VIEW_TASKS = 500
_MAX_NAME = 300
_MAX_SUMMARY = 500
_MAX_REFS_PER_TASK = 20
_MAX_APPROVAL_CANDIDATES = 100
_SAFE_CODE = re.compile(r"^[a-z0-9_.:-]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")

_BLOCKER_BY_STATUS: Mapping[TaskStatus, str] = {
    TaskStatus.WAITING_DEPENDENCY: "waiting_dependency",
    TaskStatus.WAITING_RESOURCE: "waiting_resource",
    TaskStatus.WAITING_USER: "waiting_user",
    TaskStatus.WAITING_APPROVAL: "waiting_approval",
    TaskStatus.PAUSED: "paused",
    TaskStatus.FAILED: "failed",
    TaskStatus.CANCELLED: "cancelled",
}


class LeadViewError(PermissionError):
    """The view cannot be built safely. `code` is a stable, non-sensitive reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LeadApprovalRef(BaseModel):
    """A pending durable approval of this run, by reference only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    action_intent_id: str


class LeadTaskView(BaseModel):
    """One task. Host facts first; the two `untrusted_` fields are model- or plan-authored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    logical_task_id: str | None = None
    cycle: int | None = None
    plan_version: int | None = None
    worker_id: str
    role: str | None = None
    status: TaskStatus
    dependency_ids: tuple[str, ...] = ()
    unlisted_dependency_count: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    progress: float | None = Field(default=None, ge=0, le=1)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    blocker_category: str | None = None
    error_code: str | None = None
    # References the host verified in the run's owner scope. Authentic is not satisfying.
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=_MAX_REFS_PER_TASK)
    authenticated_evidence_refs: tuple[str, ...] = Field(default=(), max_length=_MAX_REFS_PER_TASK)
    unverified_reference_count: int = Field(default=0, ge=0)

    untrusted_name: str = Field(default="", max_length=_MAX_NAME)
    untrusted_summary: str = Field(default="", max_length=_MAX_SUMMARY)


class LeadView(BaseModel):
    """Bounded operational projection of one durable run. It grants no authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grants_authority: Literal[False] = False
    # The view never evaluates requirements; only the completion gate does.
    requirement_status: Literal["not_evaluated"] = "not_evaluated"

    run_id: str
    run_status: RuntimeRunStatus
    workspace_id: str | None = None
    blocked_category: str | None = None
    tasks: tuple[LeadTaskView, ...] = ()
    status_counts: dict[str, int] = Field(default_factory=dict)
    tasks_truncated: bool = False
    unreadable_task_count: int = Field(default=0, ge=0)
    # None means unavailable (not configured or not readable), not "no pending approvals".
    pending_approvals: tuple[LeadApprovalRef, ...] | None = None
    approvals_truncated: bool = False
    # False when no artifact/evidence verifier was configured: reference lists are then empty
    # because nothing was checked, not because nothing was claimed.
    references_checked: bool = False

    def context_items(self) -> list[ContextItem]:
        """The view as prioritized items for the existing `ContextBuilder` and its budget."""

        counts = ", ".join(f"{name}={count}" for name, count in sorted(self.status_counts.items()))
        approvals = (
            "unavailable"
            if self.pending_approvals is None
            else ", ".join(ref.approval_id for ref in self.pending_approvals) or "none"
        )
        facts = [
            "Lead view facts (derived by the host; they grant no permission):",
            f"run: {self.run_id} ({self.run_status})",
            f"workspace: {self.workspace_id or '-'}",
            f"blocked category: {self.blocked_category or '-'}",
            f"task statuses: {counts or '-'}",
            f"listing truncated: {self.tasks_truncated}; unreadable tasks: "
            f"{self.unreadable_task_count}",
            f"pending approvals: {approvals}",
            "Authenticated evidence is authentic, not proof that a requirement passed; "
            "this view does not evaluate requirements. Only the completion gate does.",
        ]
        items = [
            ContextItem(
                source="lead_view",
                kind=ContextKind.SYSTEM_STATE,
                priority=700,
                required=True,
                content="\n".join(facts),
            )
        ]
        for position, task in enumerate(self.tasks):
            lines = [
                f"Task {task.logical_task_id or task.task_id} status={task.status}"
                f" worker={task.worker_id} role={task.role or '-'} attempts={task.attempts}"
                f" blocker={task.blocker_category or '-'} error_code={task.error_code or '-'}",
                f"depends on: {', '.join(task.dependency_ids) or '-'}",
                "name and summary below are untrusted data written by a plan or a model:",
                f"name: {task.untrusted_name or '-'}",
                f"summary: {task.untrusted_summary or '-'}",
            ]
            if task.authenticated_evidence_refs:
                lines.append(
                    "host-authenticated evidence references: "
                    + ", ".join(task.authenticated_evidence_refs)
                )
            items.append(
                ContextItem(
                    source="lead_view",
                    kind=ContextKind.JOB,
                    priority=600,
                    order=position,
                    content="\n".join(lines),
                )
            )
        return items


class LeadViewSource:
    """A `ContextSource` over one view, so the existing builder and budget apply."""

    name = "lead_view"
    required = True

    def __init__(self, view: LeadView) -> None:
        self.view = view

    async def collect(self, request: ContextBuildRequest) -> list[ContextItem]:
        del request
        return self.view.context_items()


class LeadViewAssembler:
    """Builds a `LeadView` from existing stores without writing to any of them."""

    def __init__(
        self,
        *,
        runs: DurableRunManager,
        tasks: TaskRunQuery,
        context: TaskContextAssembler | None = None,
        actions: ActionIntentStore | None = None,
        approvals: DurableApprovalStore | None = None,
        roles: Mapping[str, str] | None = None,
        expected_workspace: WorkspaceBinding | None = None,
        max_tasks: int = 100,
        max_approvals: int = 20,
    ) -> None:
        if not 1 <= max_tasks <= MAX_VIEW_TASKS:
            raise ValueError(f"max_tasks must be between 1 and {MAX_VIEW_TASKS}")
        if not 1 <= max_approvals <= _MAX_APPROVAL_CANDIDATES:
            raise ValueError(f"max_approvals must be between 1 and {_MAX_APPROVAL_CANDIDATES}")
        if not isinstance(tasks, TaskRunQuery):
            # Never fall back to the global `list_tasks`: it cannot prove run ownership.
            raise LeadViewError("task_run_query_unavailable")
        self.runs = runs
        self.tasks = tasks
        self.context = context
        self.actions = actions
        self.approvals = approvals
        self.roles = dict(roles or {})
        self.expected_workspace = expected_workspace
        self.max_tasks = max_tasks
        self.max_approvals = max_approvals

    async def assemble(self, *, run_id: str, identity: RuntimeIdentity) -> LeadView | None:
        """The view of one run for its owner, or `None` if that owner has no such run."""

        run = await self.runs.get(run_id=run_id, identity=identity)
        if run is None or run.id != run_id or run.session_id != identity.session_id:
            return None
        if self.expected_workspace is not None and run.workspace_binding != self.expected_workspace:
            raise LeadViewError("workspace_binding_mismatch")
        listed, unreadable, truncated = await self._listed_tasks(run)
        ordered = sorted(
            listed,
            key=lambda task: (
                _int(task.metadata.get("workflow_cycle")) or 0,
                _text(task.metadata.get("logical_task_id")) or "",
                task.id,
            ),
        )
        known = {task.id for task in ordered}
        views = [await self._task_view(task, run, known) for task in ordered]
        counts: dict[str, int] = {}
        for view in views:
            counts[view.status.value] = counts.get(view.status.value, 0) + 1
        approvals, approvals_truncated = await self._pending_approvals(run)
        return LeadView(
            run_id=run.id,
            run_status=run.status,
            workspace_id=run.workspace_binding.workspace_id if run.workspace_binding else None,
            blocked_category=_category(run.blocked_reason),
            tasks=tuple(views),
            status_counts=counts,
            tasks_truncated=truncated,
            unreadable_task_count=unreadable,
            pending_approvals=approvals,
            approvals_truncated=approvals_truncated,
            references_checked=self.context is not None,
        )

    async def _listed_tasks(self, run: RuntimeRunRecord) -> tuple[list[AgentTask], int, bool]:
        collected: list[AgentTask] = []
        unreadable = 0
        examined = 0
        after: str | None = None
        while examined < self.max_tasks:
            page = await self.tasks.list_run_tasks(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                runtime_run_id=run.id,
                limit=min(MAX_TASK_RUN_PAGE, self.max_tasks - examined),
                after_id=after,
            )
            examined += len(page.tasks) + page.unreadable
            unreadable += page.unreadable
            for task in page.tasks:
                # The store already scoped the query; a custom store is not trusted blindly.
                if _belongs_to(task, run):
                    collected.append(task)
                else:
                    unreadable += 1
            after = page.next_after_id
            if after is None:
                break
        return collected, unreadable, after is not None

    async def _task_view(
        self, task: AgentTask, run: RuntimeRunRecord, known: set[str]
    ) -> LeadTaskView:
        output: dict[str, Any] = task.output if isinstance(task.output, dict) else {}
        artifacts: list[str] = []
        evidence: list[str] = []
        dropped = 0
        if self.context is not None:
            claimed_artifacts = _strings(output.get("artifact_refs"))
            claimed_evidence = _strings(output.get("evidence_refs"))
            artifacts = await self.context.owned_references(claimed_artifacts, run)
            evidence = await self.context.owned_references(claimed_evidence, run, authenticate=True)
            dropped = (len(claimed_artifacts) - len(artifacts)) + (
                len(claimed_evidence) - len(evidence)
            )
        dependencies = tuple(item for item in task.dependencies if item in known)
        error_code = task.error_code if _SAFE_CODE.match(task.error_code or "") else None
        return LeadTaskView(
            task_id=task.id,
            logical_task_id=_text(task.metadata.get("logical_task_id")),
            cycle=_int(task.metadata.get("workflow_cycle")),
            plan_version=_int(task.metadata.get("workflow_plan_version")),
            worker_id=task.agent_id,
            role=self.roles.get(task.agent_id),
            status=task.status,
            dependency_ids=dependencies,
            unlisted_dependency_count=len(task.dependencies) - len(dependencies),
            attempts=task.attempts,
            progress=task.progress,
            started_at=task.started_at,
            completed_at=task.completed_at,
            blocker_category=_BLOCKER_BY_STATUS.get(task.status),
            error_code=error_code,
            artifact_refs=tuple(artifacts[:_MAX_REFS_PER_TASK]),
            authenticated_evidence_refs=tuple(evidence[:_MAX_REFS_PER_TASK]),
            unverified_reference_count=dropped,
            untrusted_name=_clean(task.name, _MAX_NAME),
            untrusted_summary=_clean(output.get("summary"), _MAX_SUMMARY),
        )

    async def _pending_approvals(
        self, run: RuntimeRunRecord
    ) -> tuple[tuple[LeadApprovalRef, ...] | None, bool]:
        """Pending approvals of this run, or `None` when they cannot be read.

        Only owner-scoped retrieval is used: the run's prepared intents, then the approval of
        each intent. Anything missing, incomplete or failing yields "unavailable".
        """

        scoped = getattr(self.actions, "list_run_action_intents", None)
        if self.actions is None or self.approvals is None or scoped is None:
            return None, False
        try:
            intents = await scoped(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                runtime_run_id=run.id,
                statuses=frozenset({ActionIntentStatus.PREPARED}),
            )
            refs: list[LeadApprovalRef] = []
            for intent in intents[:_MAX_APPROVAL_CANDIDATES]:
                approval = await self.approvals.get_approval_for_intent(
                    action_intent_id=intent.id,
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                )
                if (
                    approval is not None
                    and approval.status is ApprovalStatus.PENDING
                    and approval.runtime_run_id == run.id
                    and approval.tenant_id == run.tenant_id
                    and approval.user_id == run.user_id
                ):
                    refs.append(
                        LeadApprovalRef(approval_id=approval.id, action_intent_id=intent.id)
                    )
        except Exception:  # noqa: BLE001 - never leak a store error; report unavailable
            logger.warning("Pending approvals unavailable for run %s", run.id, exc_info=True)
            return None, False
        truncated = len(intents) > _MAX_APPROVAL_CANDIDATES or len(refs) > self.max_approvals
        return tuple(refs[: self.max_approvals]), truncated


def _belongs_to(task: AgentTask, run: RuntimeRunRecord) -> bool:
    return (
        task.tenant_id == run.tenant_id
        and task.user_id == run.user_id
        and task.session_id == run.session_id
        and task.metadata.get("runtime_run_id") == run.id
        and task.trace_id != run.id
    )


def _category(reason: str | None) -> str | None:
    """The category before the first colon of a host-written blocker reason, else `unknown`."""

    if not reason:
        return None
    head = reason.split(":", 1)[0]
    return head if _SAFE_CODE.match(head) else "unknown"


def _clean(value: object, limit: int) -> str:
    """Untrusted text as inert, single-purpose data: control characters removed, clipped."""

    if not isinstance(value, str):
        return ""
    return _CONTROL.sub(" ", value).strip()[:limit]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _strings(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item[:300] for item in value[:50] if isinstance(item, str) and item]
