"""Trusted execution context for workflow tasks.

A compiled workflow task carries the owner identity and the durable run id in fields the
`WorkflowCompiler` sets from the durable run; plan-authored data lives only in
`task.input`. `trusted_task_context` turns such a task into an `AgentContext` for tools,
after re-checking the identity against the durable run record. It is the primitive a
worker uses so that command evidence is attributed to the right owner and run.

It does not run agents or tools, and it grants nothing: permissions and services come from
the host that calls it, never from the task.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kaiwen_agent.context import AgentContext
from kaiwen_agent.runtime import (
    DurableRunManager,
    RuntimeIdentity,
    RuntimeRunRecord,
    RuntimeRunStatus,
)
from kaiwen_agent.tasks import AgentTask

_RESERVED_METADATA = frozenset(
    {
        "runtime_run_id",
        "workflow_cycle",
        "workflow_plan_version",
        "logical_task_id",
        "workspace_id",
        "agent_role",
    }
)


class TaskIdentityError(PermissionError):
    """The task's identity cannot be tied to an active durable run of the same owner."""


async def trusted_task_run(task: AgentTask, *, runs: DurableRunManager) -> RuntimeRunRecord:
    """The active durable run a compiled task belongs to, for the same owner.

    Raises `TaskIdentityError` unless the task names a durable run that exists for the
    same tenant, user and session and is currently running.
    """

    if not task.tenant_id or not task.user_id or not task.session_id:
        raise TaskIdentityError("Task has no trusted tenant, user, and session")
    run_id = task.metadata.get("runtime_run_id")
    if not isinstance(run_id, str) or not run_id:
        raise TaskIdentityError("Task is not bound to a durable run")
    if task.trace_id == run_id:
        raise TaskIdentityError("A trace id must be distinct from the run id")
    record = await runs.get(
        run_id=run_id,
        identity=RuntimeIdentity(
            session_id=task.session_id,
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            conversation_id=task.conversation_id,
            trace_id=task.trace_id,
        ),
    )
    if (
        record is None
        or record.id != run_id
        or record.tenant_id != task.tenant_id
        or record.user_id != task.user_id
        or record.session_id != task.session_id
    ):
        raise TaskIdentityError("Task identity does not match a durable run of this owner")
    if record.status is not RuntimeRunStatus.RUNNING:
        raise TaskIdentityError("The durable run is not running")
    return record


async def trusted_task_context(
    task: AgentTask,
    *,
    runs: DurableRunManager,
    permissions: frozenset[str],
    workspace_id: str | None = None,
    role: str | None = None,
    services: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AgentContext:
    """Build an owner-scoped `AgentContext` from a compiled workflow task.

    Raises `TaskIdentityError` unless the task names a durable run that exists for the
    same tenant, user and session and is currently running. The run id is taken from the
    durable record, never from `task.input` or any tool argument. The trace id is kept
    distinct from the run id.
    """

    reserved = _RESERVED_METADATA & set(metadata or {})
    if reserved:
        raise TaskIdentityError(f"Context metadata may not set reserved keys: {sorted(reserved)}")
    record = await trusted_task_run(task, runs=runs)
    context_metadata: dict[str, Any] = dict(metadata or {})
    context_metadata["runtime_run_id"] = record.id
    for key in ("workflow_cycle", "logical_task_id"):
        if key in task.metadata:
            context_metadata[key] = task.metadata[key]
    if workspace_id is not None:
        context_metadata["workspace_id"] = workspace_id
    if role is not None:
        context_metadata["agent_role"] = role
    return AgentContext(
        session_id=record.session_id,
        tenant_id=record.tenant_id,
        user_id=record.user_id,
        agent_id=task.agent_id,
        conversation_id=record.conversation_id,
        trace_id=task.trace_id,
        permissions=permissions,
        services=dict(services or {}),
        metadata=context_metadata,
    )
