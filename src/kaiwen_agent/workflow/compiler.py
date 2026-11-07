from __future__ import annotations

import json
from hashlib import sha256

from kaiwen_agent.runtime import RuntimeIdentity, RuntimeRunRecord
from kaiwen_agent.tasks import AgentTask, TaskGraph, TaskGraphError, TaskRetryPolicy
from kaiwen_agent.workers import WorkerNotFoundError, WorkerRegistry
from kaiwen_agent.workflow.models import WorkflowLimits, WorkflowPlan


class WorkflowPlanError(ValueError):
    pass


class WorkflowCompiler:
    """Validates an untrusted plan and compiles it into the existing TaskGraph."""

    def __init__(
        self,
        *,
        workers: WorkerRegistry,
        worker_allowlist: frozenset[str],
        effective_capabilities: frozenset[str] = frozenset(),
        resource_allowlist: frozenset[str] = frozenset(),
        limits: WorkflowLimits | None = None,
    ) -> None:
        self.workers = workers
        self.worker_allowlist = worker_allowlist
        self.effective_capabilities = effective_capabilities
        self.resource_allowlist = resource_allowlist
        self.limits = limits or WorkflowLimits()

    def compile(
        self,
        plan: WorkflowPlan,
        *,
        run: RuntimeRunRecord,
        identity: RuntimeIdentity,
        cycle: int,
    ) -> TaskGraph:
        self._validate_identity(run, identity)
        if len(plan.tasks) > self.limits.max_tasks_per_plan:
            raise WorkflowPlanError("Workflow plan exceeds the task limit")
        logical_ids = {task.id for task in plan.tasks}
        compiled_ids = {
            logical_id: _task_id(run.id, cycle, logical_id) for logical_id in logical_ids
        }
        tasks: list[AgentTask] = []
        for spec in plan.tasks:
            if spec.worker_id not in self.worker_allowlist:
                raise WorkflowPlanError(f"Worker is not allowlisted: {spec.worker_id}")
            try:
                self.workers.get(spec.worker_id)
            except WorkerNotFoundError as error:
                raise WorkflowPlanError(str(error)) from error
            unknown = set(spec.dependencies) - logical_ids
            if unknown:
                raise WorkflowPlanError(
                    f"Task {spec.id} has unknown dependencies: {sorted(unknown)}"
                )
            if len(spec.dependencies) > self.limits.max_dependencies_per_task:
                raise WorkflowPlanError(f"Task {spec.id} exceeds the dependency limit")
            if not spec.capabilities <= self.effective_capabilities:
                raise WorkflowPlanError(f"Task {spec.id} requests unavailable capabilities")
            if not set(spec.resource_keys) <= self.resource_allowlist:
                raise WorkflowPlanError(f"Task {spec.id} requests unavailable resources")
            try:
                serialized_input = json.dumps(
                    spec.input,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as error:
                raise WorkflowPlanError(f"Task {spec.id} input must be strict JSON") from error
            input_bytes = len(serialized_input.encode())
            if input_bytes > self.limits.max_input_bytes_per_task:
                raise WorkflowPlanError(f"Task {spec.id} input exceeds the size limit")
            tasks.append(
                AgentTask(
                    id=compiled_ids[spec.id],
                    name=spec.name,
                    description=spec.description,
                    agent_id=spec.worker_id,
                    session_id=identity.session_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    conversation_id=identity.conversation_id,
                    trace_id=identity.trace_id or f"trace_{run.id}",
                    dependencies=[compiled_ids[item] for item in spec.dependencies],
                    input=spec.input,
                    resource_keys=list(spec.resource_keys),
                    retry_policy=TaskRetryPolicy(**spec.retry_policy.model_dump()),
                    timeout_seconds=spec.timeout_seconds,
                    capabilities=spec.capabilities,
                    metadata={
                        "runtime_run_id": run.id,
                        "workflow_cycle": cycle,
                        "workflow_plan_version": plan.version,
                        "logical_task_id": spec.id,
                    },
                )
            )
        try:
            return TaskGraph(tasks)
        except TaskGraphError as error:
            raise WorkflowPlanError(str(error)) from error

    @staticmethod
    def _validate_identity(run: RuntimeRunRecord, identity: RuntimeIdentity) -> None:
        if (identity.tenant_id, identity.user_id, identity.session_id) != (
            run.tenant_id,
            run.user_id,
            run.session_id,
        ) or identity.conversation_id != run.conversation_id:
            raise WorkflowPlanError("Runtime identity does not own the workflow run")


def _task_id(run_id: str, cycle: int, logical_id: str) -> str:
    material = f"{run_id}\x1f{cycle}\x1f{logical_id}"
    return f"task_{sha256(material.encode()).hexdigest()}"
