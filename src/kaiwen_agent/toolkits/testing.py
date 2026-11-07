from __future__ import annotations

from pydantic import BaseModel

from kaiwen_agent.context import AgentContext
from kaiwen_agent.toolkits.commands import (
    CommandCatalog,
    CommandConfigurationError,
    CommandRunInput,
)
from kaiwen_agent.tools.definition import ToolDefinition

_UNATTENDED_ALLOWED = frozenset({"test", "lint", "typecheck"})


def testing_tools(
    catalog: CommandCatalog,
    *,
    requires_approval: bool = True,
    unattended_categories: frozenset[str] = frozenset(),
) -> tuple[ToolDefinition, ...]:
    """Create category-scoped tools over the same trusted command catalog.

    Approval is required unless the host lists a category in `unattended_categories`.
    Only `test`, `lint` and `typecheck` may run unattended, and only when the catalog's
    runner declares real isolation (`provides_isolation`), such as `DockerSandboxRunner`.
    """

    if unattended_categories:
        if not unattended_categories <= _UNATTENDED_ALLOWED:
            raise CommandConfigurationError(
                "Only test, lint and typecheck presets may run without approval"
            )
        if getattr(catalog.runner, "provides_isolation", False) is not True:
            raise CommandConfigurationError(
                "Unattended execution requires a runner that provides real isolation"
            )

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
            requires_approval=requires_approval and category not in unattended_categories,
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
