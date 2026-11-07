from __future__ import annotations

from collections.abc import Iterable

from kaiwen_agent.agents.definitions import AgentDefinition, VersionedReference


class AgentDefinitionRegistry:
    def __init__(self, definitions: Iterable[AgentDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, int], AgentDefinition] = {}
        self._latest: dict[str, int] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: AgentDefinition) -> None:
        key = (definition.id, definition.version)
        if key in self._definitions:
            raise ValueError(
                f"Agent definition already registered: {definition.id}@{definition.version}"
            )
        self._definitions[key] = definition
        self._latest[definition.id] = max(definition.version, self._latest.get(definition.id, 0))

    def get(self, agent_id: str, version: int | None = None) -> AgentDefinition:
        selected = version if version is not None else self._latest.get(agent_id)
        if selected is None:
            raise KeyError(f"Unknown agent definition: {agent_id}")
        try:
            return self._definitions[(agent_id, selected)]
        except KeyError as error:
            raise KeyError(f"Unknown agent definition: {agent_id}@{selected}") from error

    def resolve(self, reference: VersionedReference) -> AgentDefinition:
        return self.get(reference.id, reference.version)

    def list(self) -> tuple[AgentDefinition, ...]:
        return tuple(
            self._definitions[key]
            for key in sorted(self._definitions, key=lambda item: (item[0], item[1]))
        )
