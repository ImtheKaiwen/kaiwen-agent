import pytest

from kaiwen_agent.exceptions import DuplicateToolError, ToolNotFoundError
from kaiwen_agent.tools.registry import ToolRegistry

from .test_agent import create_greeting


def test_registry_denies_unknown_tools() -> None:
    registry = ToolRegistry([create_greeting])
    with pytest.raises(ToolNotFoundError):
        registry.get("admin.publish")


def test_registry_rejects_duplicate_names() -> None:
    with pytest.raises(DuplicateToolError):
        ToolRegistry([create_greeting, create_greeting])
