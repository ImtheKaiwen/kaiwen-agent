from __future__ import annotations

import string

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kaiwen_agent.tasks import AgentTask, TaskGraph, TaskGraphError


class SkillStep(BaseModel):
    """A declarative task template. Executable code is never stored in a skill."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    worker_agent_id: str = Field(min_length=1)
    description_template: str = ""
    depends_on: tuple[str, ...] = ()
    required_tools: frozenset[str] = Field(default_factory=frozenset)
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    input_template: dict[str, str] = Field(default_factory=dict)


class SkillDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    description: str = ""
    variables: frozenset[str] = Field(default_factory=frozenset)
    steps: tuple[SkillStep, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph_and_variables(self) -> SkillDefinition:
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Skill step ids must be unique")
        known = set(step_ids)
        for step in self.steps:
            unknown = set(step.depends_on) - known
            if unknown:
                raise ValueError(
                    f"Skill step {step.id} has unknown dependencies: {sorted(unknown)}"
                )
        try:
            TaskGraph(
                [
                    AgentTask(
                        id=step.id,
                        name=step.name,
                        agent_id=step.worker_agent_id,
                        dependencies=list(step.depends_on),
                    )
                    for step in self.steps
                ]
            )
        except TaskGraphError as error:
            raise ValueError(str(error)) from error
        declared: set[str] = set()
        for step in self.steps:
            declared.update(_variables(step.description_template))
            for value in step.input_template.values():
                declared.update(_variables(value))
        if declared != set(self.variables):
            raise ValueError(f"Skill variables do not match templates; expected {sorted(declared)}")
        return self


class SkillRegistry:
    def __init__(self) -> None:
        self._definitions: dict[tuple[str, int], SkillDefinition] = {}
        self._latest: dict[str, int] = {}

    def register(self, definition: SkillDefinition) -> None:
        key = (definition.id, definition.version)
        if key in self._definitions:
            raise ValueError(f"Skill version already registered: {key}")
        self._definitions[key] = definition
        self._latest[definition.id] = max(definition.version, self._latest.get(definition.id, 0))

    def get(self, skill_id: str, version: int | None = None) -> SkillDefinition:
        selected = version if version is not None else self._latest.get(skill_id)
        if selected is None:
            raise KeyError(f"Unknown skill: {skill_id}")
        try:
            return self._definitions[(skill_id, selected)]
        except KeyError as error:
            raise KeyError(f"Unknown skill version: {skill_id}@{selected}") from error

    def compile(
        self,
        skill_id: str,
        values: dict[str, str],
        *,
        session_id: str,
        trace_id: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
        conversation_id: str | None = None,
        version: int | None = None,
    ) -> TaskGraph:
        definition = self.get(skill_id, version)
        if set(values) != set(definition.variables):
            raise ValueError("Skill values must match the declared variables exactly")
        task_ids = {
            step.id: (f"skill:{trace_id}:{definition.id}:{definition.version}:{step.id}")
            for step in definition.steps
        }
        tasks = [
            AgentTask(
                id=task_ids[step.id],
                name=step.name,
                description=step.description_template.format_map(values),
                agent_id=step.worker_agent_id,
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                trace_id=trace_id,
                dependencies=[task_ids[item] for item in step.depends_on],
                input={key: value.format_map(values) for key, value in step.input_template.items()},
                capabilities=step.capabilities,
                metadata={
                    "skill_id": definition.id,
                    "skill_version": definition.version,
                    "skill_step_id": step.id,
                    "required_tools": sorted(step.required_tools),
                },
            )
            for step in definition.steps
        ]
        return TaskGraph(tasks)


def _variables(template: str) -> frozenset[str]:
    variables: set[str] = set()
    for _, field, format_spec, conversion in string.Formatter().parse(template):
        if field is None:
            continue
        if not field or "." in field or "[" in field or format_spec or conversion:
            raise ValueError("Skill variables must be simple names without formatting")
        variables.add(field)
    return frozenset(variables)
