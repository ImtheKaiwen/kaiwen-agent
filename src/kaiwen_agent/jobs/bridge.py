from __future__ import annotations

from kaiwen_agent.context import AgentContext
from kaiwen_agent.jobs.delegation import DelegationRequest, SubagentDelegator
from kaiwen_agent.jobs.manager import JobManager
from kaiwen_agent.jobs.models import AgentJob, AgentJobRef, JobContext


class JobBridgeIdentityError(PermissionError):
    pass


class TextJobBridge:
    """Maps a trusted text-agent context onto the shared job/delegation runtime."""

    def __init__(self, *, jobs: JobManager, delegator: SubagentDelegator) -> None:
        self.jobs = jobs
        self.delegator = delegator

    async def delegate(self, context: AgentContext, request: DelegationRequest) -> AgentJobRef:
        return await self.delegator.delegate(self._context(context), request)

    async def status(self, context: AgentContext, job_id: str) -> AgentJob:
        return await self.jobs.get(self._context(context), job_id)

    @staticmethod
    def _context(context: AgentContext) -> JobContext:
        if context.tenant_id is None or context.user_id is None or context.agent_id is None:
            raise JobBridgeIdentityError(
                "Text job delegation requires trusted tenant, user, and agent identity"
            )
        raw_depth = context.metadata.get("delegation_depth", 0)
        if isinstance(raw_depth, bool) or not isinstance(raw_depth, int) or raw_depth < 0:
            raise JobBridgeIdentityError("Trusted delegation_depth must be a non-negative integer")
        raw_run_id = context.metadata.get("run_id")
        if raw_run_id is not None and (not isinstance(raw_run_id, str) or not raw_run_id):
            raise JobBridgeIdentityError("Trusted run_id must be a non-empty string")
        return JobContext(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            conversation_id=context.conversation_id,
            agent_id=context.agent_id,
            trace_id=context.trace_id,
            capabilities=context.permissions,
            delegation_depth=raw_depth,
            run_id=raw_run_id,
        )
