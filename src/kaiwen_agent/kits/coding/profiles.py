from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.runtime import AutonomyLevel
from kaiwen_agent.workflow import WorkflowLimits


class CodingProfile(StrEnum):
    FAST = "fast"
    BALANCED = "balanced"
    THOROUGH = "thorough"


class CodingMode(StrEnum):
    MANUAL = "manual"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"

    @property
    def autonomy(self) -> AutonomyLevel:
        return AutonomyLevel(self.value)


class CodingProfileSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_agent_steps: int = Field(ge=1, le=100)
    workflow_limits: WorkflowLimits
    prompt_variant: str
    recommended_roles: tuple[str, ...]


def coding_profile_settings(profile: CodingProfile) -> CodingProfileSettings:
    if profile is CodingProfile.FAST:
        return CodingProfileSettings(
            max_agent_steps=6,
            workflow_limits=WorkflowLimits(max_tasks_per_plan=8, max_cycles=2),
            prompt_variant="compact",
            recommended_roles=("solo",),
        )
    if profile is CodingProfile.THOROUGH:
        return CodingProfileSettings(
            max_agent_steps=20,
            workflow_limits=WorkflowLimits(max_tasks_per_plan=40, max_cycles=5),
            prompt_variant="full",
            recommended_roles=("planner", "coder", "tester", "reviewer"),
        )
    return CodingProfileSettings(
        max_agent_steps=12,
        workflow_limits=WorkflowLimits(max_tasks_per_plan=20, max_cycles=3),
        prompt_variant="full",
        recommended_roles=("solo",),
    )
