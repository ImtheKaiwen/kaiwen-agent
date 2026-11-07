from __future__ import annotations

from enum import StrEnum
from importlib.resources import files

from pydantic import BaseModel, ConfigDict

from kaiwen_agent.prompts import PromptDefinition, PromptRegistry

PROMPT_VERSION = 1
_ROLES = (
    "solo",
    "lead",
    "planner",
    "architect",
    "designer",
    "coder",
    "tester",
    "reviewer",
    "devops",
)


class CodingUserLanguage(StrEnum):
    ENGLISH = "en"
    TURKISH = "tr"


class CodingConversationPreferences(BaseModel):
    """Typed runtime context; it is not interpolated into system prompt templates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_language: CodingUserLanguage = CodingUserLanguage.ENGLISH
    technical_documentation_language: CodingUserLanguage = CodingUserLanguage.ENGLISH

    @property
    def response_language(self) -> str:
        return "Turkish" if self.user_language == CodingUserLanguage.TURKISH else "English"


def coding_prompt_definitions() -> tuple[PromptDefinition, ...]:
    base = _load("base")
    definitions = [
        PromptDefinition(
            id="coding.base",
            version=PROMPT_VERSION,
            template=base,
            description="Shared professional software engineering instructions.",
        )
    ]
    for role in _ROLES:
        role_text = _load(role)
        definitions.append(
            PromptDefinition(
                id=f"coding.{role}",
                version=PROMPT_VERSION,
                template=f"{base}\n\nROLE-SPECIFIC INSTRUCTIONS\n{role_text}",
                description=f"Professional coding instructions for the {role} role.",
            )
        )
    definitions.append(
        PromptDefinition(
            id="coding.compact",
            version=PROMPT_VERSION,
            template=_load("compact"),
            description="Compact professional instructions for constrained local models.",
        )
    )
    return tuple(definitions)


def install_coding_prompts(registry: PromptRegistry) -> tuple[PromptDefinition, ...]:
    """Install immutable prompts, accepting only byte-identical existing versions."""

    definitions = coding_prompt_definitions()
    for definition in definitions:
        try:
            existing = registry.get(definition.id, definition.version)
        except KeyError:
            registry.register(definition)
            continue
        if existing != definition:
            raise ValueError(
                "Conflicting coding prompt already registered: "
                f"{definition.id}@{definition.version}"
            )
    return definitions


def _load(name: str) -> str:
    resource = files("kaiwen_agent.kits.coding.prompts").joinpath(f"{name}.md")
    return resource.read_text(encoding="utf-8").strip()
