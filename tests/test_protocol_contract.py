import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kaiwen_agent.events import AgentEvent

FIXTURES = Path(__file__).parents[1] / "packages" / "protocol" / "fixtures"


def read_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_python_accepts_canonical_valid_event_fixture() -> None:
    event = AgentEvent.model_validate(read_fixture("event.valid.json"))
    assert event.schema_version == "1.0"
    assert event.type == "run.started"
    assert event.tenant_id == "tenant_fixture"
    assert event.user_id == "user_fixture"


def test_python_rejects_canonical_invalid_event_fixture() -> None:
    with pytest.raises(ValidationError):
        AgentEvent.model_validate(read_fixture("event.invalid.json"))


def test_agent_event_redacts_secrets_at_the_contract_boundary() -> None:
    event = AgentEvent(
        type="run.failed",
        trace_id="trace_1",
        session_id="session_1",
        payload={
            "authorization": "Bearer private-value",
            "message": "Provider rejected sk-abcdefghijklmnop",
        },
    )

    assert event.payload == {
        "authorization": "[REDACTED]",
        "message": "Provider rejected [REDACTED]",
    }
