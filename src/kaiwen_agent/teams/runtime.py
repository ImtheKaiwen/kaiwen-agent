from __future__ import annotations

from typing import Any

from kaiwen_agent.agents import AgentDefinitionRegistry
from kaiwen_agent.jobs import (
    AgentJobRef,
    DelegationRequest,
    JobContext,
    JobManager,
    SubagentDelegator,
)
from kaiwen_agent.teams.models import (
    TeamDefinition,
    TeamDelegationRequest,
    TeamHandoff,
    TeamMemberDefinition,
)


class TeamCompositionError(ValueError):
    pass


class TeamRuntime:
    """Optional facade over registered agents, SubagentDelegator, and JobManager."""

    def __init__(
        self,
        *,
        definition: TeamDefinition,
        agents: AgentDefinitionRegistry,
        delegator: SubagentDelegator,
        jobs: JobManager,
    ) -> None:
        self.definition = definition
        self.agents = agents
        self.delegator = delegator
        self.jobs = jobs
        self._members = {member.role: member for member in definition.members}
        self._validate_definition()

    async def delegate(self, context: JobContext, request: TeamDelegationRequest) -> AgentJobRef:
        self._validate_caller(context, parent_job_id=request.parent_job_id)
        member = self.select_member(
            role=request.role,
            required_capabilities=request.requested_capabilities,
        )
        context_refs = {item.name: item.reference for item in request.context_refs}
        context_refs.update(
            {
                "kaiwen.team": f"{self.definition.id}@{self.definition.version}",
                "kaiwen.agent": f"{member.agent_ref.id}@{member.agent_ref.version}",
                "kaiwen.role": member.role,
            }
        )
        return await self.delegator.delegate(
            context,
            DelegationRequest(
                worker_agent_id=member.agent_ref.id,
                objective=request.objective,
                input=request.input,
                context_ref=context_refs,
                requested_capabilities=request.requested_capabilities,
                parent_job_id=request.parent_job_id,
                priority=request.priority,
                timeout_seconds=request.timeout_seconds,
                resource_keys=list(request.resource_keys),
            ),
        )

    async def wait_handoff(self, context: JobContext, job_id: str) -> TeamHandoff:
        job = await self.jobs.wait(context, job_id)
        member = self._member_for_agent(job.worker_agent_id)
        expected_team = f"{self.definition.id}@{self.definition.version}"
        expected_agent = f"{member.agent_ref.id}@{member.agent_ref.version}"
        if (
            job.context_ref.get("kaiwen.team") != expected_team
            or job.context_ref.get("kaiwen.agent") != expected_agent
            or job.context_ref.get("kaiwen.role") != member.role
        ):
            raise TeamCompositionError("Job is not pinned to this team definition")
        result = job.result if isinstance(job.result, dict) else {}
        return TeamHandoff(
            job_id=job.id,
            role=member.role,
            agent_ref=member.agent_ref,
            status=job.status,
            summary=_optional_text(result.get("summary")),
            artifact_refs=_string_tuple(result.get("artifact_refs")),
            evidence_refs=_string_tuple(result.get("evidence_refs")),
            blockers=_string_tuple(result.get("blockers")),
            follow_up_suggestions=_string_tuple(result.get("follow_up_suggestions")),
        )

    def select_member(
        self, *, role: str | None, required_capabilities: frozenset[str]
    ) -> TeamMemberDefinition:
        if role is not None:
            try:
                member = self._members[role]
            except KeyError as error:
                raise TeamCompositionError(f"Unknown team role: {role}") from error
            if not required_capabilities <= member.capabilities:
                raise TeamCompositionError("Requested capabilities exceed the selected role")
            return member
        candidates = [
            member
            for member in self.definition.members
            if required_capabilities <= member.capabilities
        ]
        if not candidates:
            raise TeamCompositionError("No registered team member satisfies the request")
        return min(
            candidates,
            key=lambda item: (
                len(item.capabilities - required_capabilities),
                item.role,
                item.agent_ref.id,
                item.agent_ref.version,
            ),
        )

    def _validate_definition(self) -> None:
        self.agents.resolve(self.definition.lead_agent_ref)
        for member in self.definition.members:
            agent = self.agents.resolve(member.agent_ref)
            if not member.capabilities <= agent.permissions:
                raise TeamCompositionError(f"Team role {member.role} exceeds agent permissions")

    def _validate_caller(self, context: JobContext, *, parent_job_id: str | None) -> None:
        if context.agent_id == self.definition.lead_agent_ref.id:
            return
        member = self._member_for_agent(context.agent_id)
        if not member.may_delegate:
            raise TeamCompositionError("The calling team member may not delegate")
        if parent_job_id is None:
            raise TeamCompositionError("A child team member must delegate from its parent job")

    def _member_for_agent(self, agent_id: str) -> TeamMemberDefinition:
        matches = [member for member in self.definition.members if member.agent_ref.id == agent_id]
        if len(matches) != 1:
            raise TeamCompositionError("Agent is not an unambiguous member of this team")
        return matches[0]


def _optional_text(value: Any) -> str | None:
    return value[:4_000] if isinstance(value, str) and value else None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item[:2_000] for item in value[:100] if isinstance(item, str) and item)
