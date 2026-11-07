from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from kaiwen_agent.realtime.manager import RealtimeSessionManager
from kaiwen_agent.tasks.models import AgentTask, TaskStatus


class RealtimeTaskPermissionError(PermissionError):
    pass


class RealtimeTaskNotFoundError(LookupError):
    pass


class TaskReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: TaskStatus
    agent_id: str


class TaskSubmissionPort(Protocol):
    async def submit(self, task: AgentTask) -> None: ...

    async def get(self, task_id: str) -> AgentTask | None: ...


class InMemoryTaskSubmissionPort:
    def __init__(self) -> None:
        self._tasks: dict[str, AgentTask] = {}

    async def submit(self, task: AgentTask) -> None:
        if task.id in self._tasks:
            raise ValueError(f"Task already submitted: {task.id}")
        self._tasks[task.id] = task.model_copy(deep=True)

    async def get(self, task_id: str) -> AgentTask | None:
        task = self._tasks.get(task_id)
        return task.model_copy(deep=True) if task is not None else None


class RealtimeTaskBridge:
    """Submits long work without blocking the realtime conversation."""

    def __init__(
        self,
        sessions: RealtimeSessionManager,
        submissions: TaskSubmissionPort,
        *,
        allowed_agents: set[str] | frozenset[str],
        default_agent: str,
    ) -> None:
        self._sessions = sessions
        self._submissions = submissions
        self._allowed_agents = frozenset(allowed_agents)
        if default_agent not in self._allowed_agents:
            raise ValueError("default_agent must be included in allowed_agents")
        self._default_agent = default_agent

    async def delegate(
        self,
        session_id: str,
        request: str,
        *,
        preferred_agent: str | None = None,
    ) -> TaskReference:
        if not request.strip():
            raise ValueError("request must not be empty")
        session = await self._sessions.get_session(session_id)
        agent_id = preferred_agent or self._default_agent
        if agent_id not in self._allowed_agents:
            raise RealtimeTaskPermissionError(f"Agent is not allowed: {agent_id}")
        task = AgentTask(
            name=request.strip()[:80],
            description=request.strip(),
            agent_id=agent_id,
            session_id=session.id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            conversation_id=session.conversation_id,
            input={"request": request.strip()},
        )
        await self._submissions.submit(task)
        return TaskReference(task_id=task.id, status=task.status, agent_id=task.agent_id)

    async def status(self, session_id: str, task_id: str) -> TaskReference:
        session = await self._sessions.get_session(session_id)
        task = await self._submissions.get(task_id)
        if task is None:
            raise RealtimeTaskNotFoundError(f"Task not found: {task_id}")
        if task.tenant_id != session.tenant_id or task.user_id != session.user_id:
            raise RealtimeTaskNotFoundError(f"Task not found: {task_id}")
        return TaskReference(task_id=task.id, status=task.status, agent_id=task.agent_id)
