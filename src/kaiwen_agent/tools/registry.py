from collections.abc import Iterable

from kaiwen_agent.exceptions import DuplicateToolError, ToolNotFoundError
from kaiwen_agent.tools.definition import ToolDefinition


class ToolRegistry:
    def __init__(self, tools: Iterable[ToolDefinition] = ()) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for definition in tools:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise DuplicateToolError(f"Tool already registered: {definition.name}")
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as error:
            raise ToolNotFoundError(f"Unknown tool: {name}") from error

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._tools.values())
