from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.jobs.manager import JobManager
from kaiwen_agent.jobs.models import AgentJobRef, JobContext, JobSubmitRequest
from kaiwen_agent.policy import PolicyContext, PolicyDecisionType, PolicyEngine


class DelegationDeniedError(PermissionError):
    pass


class DelegationApprovalRequiredError(PermissionError):
    pass


class DelegationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_agent_id: str = Field(min_length=1)
    objective: str = Field(min_length=1, max_length=2_000)
    input: dict[str, Any] = Field(default_factory=dict)
    context_ref: dict[str, str] = Field(default_factory=dict)
    requested_capabilities: frozenset[str] = Field(default_factory=frozenset)
    parent_job_id: str | None = None
    priority: int = Field(default=0, ge=-1000, le=1000)
    timeout_seconds: float | None = Field(default=None, gt=0)
    resource_keys: list[str] = Field(default_factory=list)


class SubagentDelegator:
    """Fail-closed delegation boundary layered on the shared JobManager."""

    def __init__(self, *, jobs: JobManager, policy_engine: PolicyEngine) -> None:
        self.jobs = jobs
        self.policy_engine = policy_engine
        self._submission_lock = asyncio.Lock()

    async def delegate(self, context: JobContext, request: DelegationRequest) -> AgentJobRef:
        async with self._submission_lock:
            return await self._delegate(context, request)

    async def _delegate(self, context: JobContext, request: DelegationRequest) -> AgentJobRef:
        if not request.requested_capabilities <= context.capabilities:
            raise DelegationDeniedError("Requested capabilities exceed caller capabilities")
        parent = None
        current_depth = context.delegation_depth
        if request.parent_job_id is not None:
            parent = await self.jobs.get(context, request.parent_job_id)
            if parent.worker_agent_id != context.agent_id:
                raise DelegationDeniedError("Only the assigned worker may delegate a child")
            if context.delegation_depth != parent.delegation_depth:
                raise DelegationDeniedError("Caller delegation depth does not match parent")
            if not request.requested_capabilities <= parent.capabilities:
                raise DelegationDeniedError("Requested capabilities exceed parent job capabilities")
            current_depth = parent.delegation_depth

        parallel = await self.jobs.store.count_jobs(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            requested_by_agent_id=context.agent_id,
            active_only=True,
        )
        jobs_in_run = 0
        if context.run_id is not None:
            jobs_in_run = await self.jobs.store.count_jobs(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                requested_by_agent_id=context.agent_id,
                parent_run_id=context.run_id,
            )
        decision = await self.policy_engine.evaluate(
            PolicyContext(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                agent_id=context.agent_id,
                session_id=context.session_id,
                conversation_id=context.conversation_id,
                run_id=context.run_id,
                trace_id=context.trace_id or "delegation",
                action="agent.delegate",
                resource=request.worker_agent_id,
                capabilities=context.capabilities,
                metadata={
                    "delegation_depth": current_depth,
                    "parallel_jobs": parallel,
                    "jobs_in_run": jobs_in_run,
                    "requested_capabilities": sorted(request.requested_capabilities),
                    "parent_job_id": request.parent_job_id,
                },
            )
        )
        if decision.decision in {
            PolicyDecisionType.REQUIRE_APPROVAL,
            PolicyDecisionType.REVIEW,
            PolicyDecisionType.MODIFY,
        }:
            raise DelegationApprovalRequiredError(decision.reason)
        if decision.decision != PolicyDecisionType.ALLOW:
            raise DelegationDeniedError(decision.reason)
        return await self.jobs.submit_delegated(
            context,
            JobSubmitRequest(
                worker_agent_id=request.worker_agent_id,
                objective=request.objective,
                input=request.input,
                context_ref=request.context_ref,
                parent_job_id=request.parent_job_id,
                priority=request.priority,
                timeout_seconds=request.timeout_seconds,
                resource_keys=request.resource_keys,
            ),
            capabilities=request.requested_capabilities,
            delegation_depth=current_depth + 1,
        )
