from __future__ import annotations

from pathlib import Path

import pytest

import kaiwen_agent
from kaiwen_agent.kits.coding import (
    CodingConversationPreferences,
    CodingUserLanguage,
    coding_prompt_definitions,
    install_coding_prompts,
)
from kaiwen_agent.prompts import PromptDefinition, PromptRegistry


def test_coding_prompts_are_complete_english_versioned_and_composed() -> None:
    definitions = coding_prompt_definitions()
    by_id = {definition.id: definition for definition in definitions}
    assert set(by_id) == {
        "coding.base",
        "coding.solo",
        "coding.lead",
        "coding.planner",
        "coding.architect",
        "coding.designer",
        "coding.coder",
        "coding.tester",
        "coding.reviewer",
        "coding.devops",
        "coding.compact",
    }
    assert all(definition.version == 1 for definition in definitions)
    assert all(definition.variables == frozenset() for definition in definitions)
    assert "Only claim completion" in by_id["coding.base"].template
    assert by_id["coding.base"].template in by_id["coding.solo"].template
    assert "do not require subagents" in by_id["coding.solo"].template
    assert "trusted completion gate" in by_id["coding.lead"].template
    assert "answer the user in their chosen language" in by_id["coding.compact"].template


def test_coding_prompt_installer_is_idempotent_only_for_identical_versions() -> None:
    registry = PromptRegistry()
    installed = install_coding_prompts(registry)
    assert install_coding_prompts(registry) == installed

    conflicting = PromptRegistry()
    conflicting.register(
        PromptDefinition(id="coding.base", version=1, template="Conflicting instructions")
    )
    with pytest.raises(ValueError, match="Conflicting coding prompt"):
        install_coding_prompts(conflicting)


def test_turkish_user_language_is_typed_without_translating_system_prompts() -> None:
    preferences = CodingConversationPreferences(user_language=CodingUserLanguage.TURKISH)
    assert preferences.response_language == "Turkish"
    assert preferences.technical_documentation_language == CodingUserLanguage.ENGLISH
    assert (
        "Respond to the user in their configured language"
        in coding_prompt_definitions()[0].template
    )


def test_coding_prompts_are_opt_in_and_not_imported_by_package_root() -> None:
    package_root = Path(kaiwen_agent.__file__).read_text(encoding="utf-8")
    assert "kaiwen_agent.kits.coding" not in package_root
    assert "kaiwen_agent.toolkits" not in package_root
