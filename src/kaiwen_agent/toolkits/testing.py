from __future__ import annotations

from pydantic import BaseModel

from kaiwen_agent.context import AgentContext
from kaiwen_agent.toolkits.commands import CommandCatalog, CommandRunInput
from kaiwen_agent.tools.definition import ToolDefinition


def testing_tools(
    catalog: CommandCatalog,
    *,
    requires_approval: bool = True,
) -> tuple[ToolDefinition, ...]:
    """Create category-scoped tools over the same trusted command catalog."""

    def definition(
        *,
        name: str,
        description: str,
        category: str,
        permission: str,
    ) -> ToolDefinition:
        async def run(context: AgentContext, arguments: BaseModel) -> object:
            if not isinstance(arguments, CommandRunInput):
                raise TypeError("Testing command arguments were not validated")
            return (await catalog.execute(context, arguments, category=category)).model_dump(
                mode="json"
            )

        return ToolDefinition(
            name=name,
            description=description,
            input_model=CommandRunInput,
            handler=run,
            permissions=frozenset({permission}),
            side_effect="reversible",
            requires_approval=requires_approval,
            idempotent=False,
            timeout_seconds=3_700,
        )

    return (
        definition(
            name="testing.run",
            description="Run one host-registered test preset in the isolated runner.",
            category="test",
            permission="tests.run",
        ),
        definition(
            name="quality.lint",
            description="Run one host-registered lint preset in the isolated runner.",
            category="lint",
            permission="tests.run",
        ),
        definition(
            name="quality.typecheck",
            description="Run one host-registered typecheck preset in the isolated runner.",
            category="typecheck",
            permission="tests.run",
        ),
        definition(
            name="build.run",
            description="Run one host-registered build preset in the isolated runner.",
            category="build",
            permission="build.run",
        ),
    )
