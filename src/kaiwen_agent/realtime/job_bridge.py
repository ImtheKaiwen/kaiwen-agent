from __future__ import annotations

from collections.abc import Callable

from kaiwen_agent.jobs import (
    AgentJob,
    AgentJobRef,
    DelegationRequest,
    JobContext,
    JobManager,
    SubagentDelegator,
)
from kaiwen_agent.realtime.manager import RealtimeSessionManager
from kaiwen_agent.realtime.session import RealtimeSessionRecord

RealtimeCapabilityResolver = Callable[[RealtimeSessionRecord], frozenset[str]]


class RealtimeJobPermissionError(PermissionError):
    pass


class RealtimeJobBridge:
    """Maps a trusted live session onto the same JobManager used by text agents."""

    def __init__(
        self,
        sessions: RealtimeSessionManager,
        *,
        jobs: JobManager,
        delegator: SubagentDelegator,
        agent_id: str,
        capability_resolver: RealtimeCapabilityResolver,
    ) -> None:
        if not agent_id:
            raise ValueError("agent_id cannot be empty")
        self.sessions = sessions
        self.jobs = jobs
        self.delegator = delegator
        self.agent_id = agent_id
        self.capability_resolver = capability_resolver

    async def delegate(self, session_id: str, request: DelegationRequest) -> AgentJobRef:
        session = await self.sessions.get_session(session_id)
        if not session.config.task_delegation:
            raise RealtimeJobPermissionError("Task delegation is disabled for this session")
        return await self.delegator.delegate(self._context(session), request)

    async def status(self, session_id: str, job_id: str) -> AgentJob:
        session = await self.sessions.get_session(session_id)
        return await self.jobs.get(self._context(session), job_id)

    def _context(self, session: RealtimeSessionRecord) -> JobContext:
        return JobContext(
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            session_id=session.id,
            conversation_id=session.conversation_id,
            agent_id=self.agent_id,
            trace_id=f"realtime:{session.id}",
            capabilities=self.capability_resolver(session),
        )
