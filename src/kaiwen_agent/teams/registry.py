from __future__ import annotations

from collections.abc import Iterable

from kaiwen_agent.teams.models import TeamDefinition


class TeamDefinitionRegistry:
    def __init__(self, definitions: Iterable[TeamDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, int], TeamDefinition] = {}
        self._latest: dict[str, int] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: TeamDefinition) -> None:
        key = (definition.id, definition.version)
        if key in self._definitions:
            raise ValueError(
                f"Team definition already registered: {definition.id}@{definition.version}"
            )
        self._definitions[key] = definition
        self._latest[definition.id] = max(definition.version, self._latest.get(definition.id, 0))

    def get(self, team_id: str, version: int | None = None) -> TeamDefinition:
        selected = version if version is not None else self._latest.get(team_id)
        if selected is None:
            raise KeyError(f"Unknown team definition: {team_id}")
        try:
            return self._definitions[(team_id, selected)]
        except KeyError as error:
            raise KeyError(f"Unknown team definition: {team_id}@{selected}") from error

    def list(self) -> tuple[TeamDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))
