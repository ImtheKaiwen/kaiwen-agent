"""M6.4-B: readiness for a future, separately authorized live multi-role validation.

Nothing here calls a model, reads a key or needs Docker. The offline `team-plan` command builds the
exact tool schemas the model-driven roles would send, checks strict tool-calling compatibility,
computes the request ceiling and the worst-case spend, and prints the authorization request. The
example's `model_factory` seam is the only thing a live harness would need to put a real provider
behind the same team. A deterministic result here is NOT evidence that a live provider works.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kaiwen_agent.types import ModelResponse
from kaiwen_agent.workflow import WorkflowOutcomeStatus
from scripts import live_validation as lv
from tests.test_coding_team_runner import run

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "coding-team-acceptance" / "main.py"
PRICES = ["--price-input-per-mtok", "0.75", "--price-output-per-mtok", "4.5"]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("coding_team_acceptance_example_live", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


example = load()


# ------------------------------------------------------------------------------- team-plan


def plan(**limits: Any) -> dict[str, Any]:
    settings: dict[str, Any] = {"max_requests": 24, "budget_usd": 0.5}
    settings.update(limits)
    base = lv.Limits(max_input_tokens=8_000, max_output_tokens=1_500, **settings)
    return lv.team_plan(base, lv.Pricing(0.75, 4.5), "gpt-5.4-mini")


def test_the_plan_is_offline_and_lists_the_exact_strict_tool_set() -> None:
    result = plan()
    assert result["executed"] is False and result["verdict"] == "READY_FOR_AUTHORIZATION_REQUEST"
    assert set(result["tools"]) == {
        "lead__view",
        "lead__propose_task",
        "review__submit",
        "testing__run",
    }
    assert result["strict_schema_issues"] == []
    # `testing.run` carries a free-form parameters mapping, so it is sent non-strict; the host's own
    # argument validation is unchanged. This is a documented compatibility fact, not a defect.
    assert result["tools"]["testing__run"] is False and result["tools"]["review__submit"] is True
    assert result["expected_requests"] == 18 and result["worst_case_cost_usd"] == pytest.approx(
        0.306
    )


@pytest.mark.parametrize(
    ("limits", "pricing", "blocker"),
    [
        ({"max_requests": 12}, lv.Pricing(0.75, 4.5), "below the 18 the plan needs"),
        ({"budget_usd": 0.1}, lv.Pricing(0.75, 4.5), "exceeds the budget"),
        ({}, lv.Pricing(0.0, 0.0), "prices were not provided"),
    ],
    ids=["too_few_requests", "over_budget", "unknown_prices"],
)
def test_an_unsafe_plan_is_never_ready_for_an_authorization_request(
    limits: dict[str, Any], pricing: lv.Pricing, blocker: str
) -> None:
    settings: dict[str, Any] = {"max_requests": 24, "budget_usd": 0.5}
    settings.update(limits)
    result = lv.team_plan(
        lv.Limits(max_input_tokens=8_000, max_output_tokens=1_500, **settings), pricing, "m"
    )
    assert result["verdict"] == "NOT_READY"
    assert any(blocker in text for text in result["blockers"])


def test_an_open_object_in_a_strict_tool_schema_blocks_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_tool = {
        "name": "lead__propose_task",
        "strict": True,
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
    }
    monkeypatch.setattr(lv, "probe_team_tools", lambda: [open_tool])
    result = plan()
    assert result["verdict"] == "NOT_READY" and result["strict_schema_issues"]
    assert any("open object" in text for text in result["blockers"])


def test_the_command_line_plan_calls_no_model_and_reports_its_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "sk-test-0123456789abcdefghij"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the plan must not build a provider or a client")

    monkeypatch.setattr(lv, "_real_client", forbidden)
    monkeypatch.setattr(lv, "build_provider", forbidden)
    report = tmp_path / "plan.json"
    assert lv.main(["team-plan", *PRICES, "--budget-usd", "0.5", "--report", str(report)]) == 0
    output = capsys.readouterr()
    assert (
        secret not in output.out and secret not in output.err and secret not in report.read_text()
    )
    saved = json.loads(report.read_text())
    assert saved["executed"] is False and saved["authorization_request"]["retries"] == "none"
    assert "no model was called" in output.err
    # Without prices the worst-case spend is unknown, so the command refuses to call it ready.
    assert lv.main(["team-plan"]) == 1
    assert lv.main(["team-plan", "--max-requests", "0"]) == 2  # invalid limits are refused


# --------------------------------------------------------------------- model_factory seam


def test_the_model_factory_supplies_every_scripted_role_and_changes_nothing_else(
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    def factory(role: str, steps: list[Any]) -> Any:
        seen.append(role)
        return example.ScriptedModel(*steps)

    result = run(example.run_scenario(example.SCENARIOS[0], tmp_path, model_factory=factory))
    assert result.outcome.status is WorkflowOutcomeStatus.COMPLETED
    assert sorted(seen) == ["lead", "lead2", "reviewer"]  # the Tester is a host step here


def test_a_model_that_only_claims_approval_cannot_complete_the_delivery(tmp_path: Path) -> None:
    class Claims:
        """A provider stand-in that answers in prose only: no tool call, no assessment."""

        async def respond(self, agent_input: Any, **kwargs: Any) -> ModelResponse:
            return ModelResponse(text="Approved. Everything looks great and all tests passed.")

    def factory(role: str, steps: list[Any]) -> Any:
        return Claims() if role == "reviewer" else example.ScriptedModel(*steps)

    result = run(example.run_scenario(example.SCENARIOS[0], tmp_path, model_factory=factory))
    assert result.outcome.status is not WorkflowOutcomeStatus.COMPLETED
    assert result.run_status != "completed"  # a claim in prose is not a review record
