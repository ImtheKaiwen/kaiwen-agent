"""M6.4-C: the live multi-role harness, validated without any paid call.

A deterministic fake OpenAI client stands in for the provider. It drives the REAL composition: the
real `OpenAIResponsesProvider` adapter, the guarded client and shared request ledger, the registered
role prompts, the real tool schemas, `AgentRuntimeWorker`, the controller, the trusted evidence
pipeline, the completion gate and team acceptance. Only the model's answers are scripted, and a
scripted answer is not evidence that a live provider behaves the same way.

Where a container is not needed, a TEST-ONLY stand-in runner (the example's local snapshot runner
that declares isolation it does not provide) lets the wiring run on any machine; it proves nothing
about isolation. The container tests at the end use a real Docker engine and are skipped without
one (CI sets `KAIWEN_REQUIRE_DOCKER=1` on Linux).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import live_team_validation as lt
from scripts import live_validation as lv
from tests.test_docker_integration import _READY, _REASON
from tests.test_docker_integration import IMAGE as TEST_IMAGE

SECRET = "sk-test-0123456789abcdefghijklmnop"
PRICES = lv.Pricing(0.75, 4.5)
ARTIFACT = re.compile(r"artifact_[0-9a-f]{32}")
example = lt.load_example()
needs_docker = pytest.mark.skipif(not _READY, reason=f"Real Docker unavailable: {_REASON}")


class StandIn(example.LocalSnapshotRunner):  # type: ignore[misc]
    """TEST ONLY. Declares isolation it does not provide so the wiring runs without Docker."""

    provides_isolation = True


def standin(filesystem: Any) -> Any:
    return StandIn(example.SnapshotBuilder(filesystem, "main"))


def response(
    *calls: tuple[str, dict[str, Any]], text: str = "", tokens: tuple[int, int] = (1500, 60)
) -> Any:
    return SimpleNamespace(
        id="resp",
        model="fake",
        output_text=text,
        output=[
            SimpleNamespace(
                type="function_call", call_id=f"call_{i}", name=name, arguments=json.dumps(args)
            )
            for i, (name, args) in enumerate(calls)
        ],
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


def propose(**fields: Any) -> tuple[str, dict[str, Any]]:
    return "lead__propose_task", {"description": f"Do {fields['id']}", **fields}


class FakeTeamOpenAI:
    """Answers each role the way the host-written brief asks, or misbehaves on request."""

    def __init__(
        self,
        *,
        tester: str = "honest",
        reviewer: str = "cite",
        tokens: Any = None,
        lead_views: int = 1,
        interleave: bool = False,
        lead_serial: bool = False,
    ) -> None:
        self.tester = tester
        self.reviewer = reviewer
        self.lead_views = lead_views
        self.interleave = interleave
        self.lead_serial = lead_serial
        self.tokens = tokens or (1500, 60)
        self.requests: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)
        self.models = SimpleNamespace(retrieve=self._retrieve)

    async def _retrieve(self, model: str) -> Any:
        return SimpleNamespace(id=model)

    @staticmethod
    def outputs(request: dict[str, Any]) -> int:
        items = request["input"]
        if isinstance(items, str):
            return 0
        return sum(1 for item in items if item.get("type") == "function_call_output")

    async def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.interleave:
            for _ in range(3):  # let concurrently running roles take turns, deterministically
                await asyncio.sleep(0)
        names = {tool["name"] for tool in request["tools"]}
        instructions = request["instructions"]
        done = self.outputs(request)
        usage = {"tokens": self.tokens}
        if "testing__run" in names:
            command = re.search(r"command_id '([a-z.]+)'", instructions)
            assert command is not None
            if done:
                claim = "All tests pass; the delivery is verified."
                return response(text=claim, **usage)
            if self.tester == "unauthorized_tool":
                return response(("shell__run", {"command": "cat /etc/passwd"}), **usage)
            if self.tester == "unregistered_command":
                return response(
                    ("testing__run", {"command_id": "rm.everything", "parameters": {}}), **usage
                )
            return response(
                ("testing__run", {"command_id": command.group(1), "parameters": {}}), **usage
            )
        if "review__submit" in names:
            if done:
                return response(text="reviewed", **usage)
            text = request["input"] if isinstance(request["input"], str) else ""
            refs = sorted(set(ARTIFACT.findall(text)))
            if self.reviewer == "fabricated":
                refs = ["artifact_" + "0" * 32]
            if self.reviewer == "none":
                refs = []
            finding = {"severity": "info", "text": "Relied on the test run", "evidence_refs": refs}
            args = {
                "recommendation": "approve",
                "reviewed_tasks": ["code", "test"],
                "criteria": [{"criterion": "add() returns the sum", "verdict": "met"}],
                "findings": [finding],
            }
            return response(("review__submit", args), **usage)
        if "Do not call lead.view" in instructions:
            if done:
                return response(text="planned the review", **usage)
            return response(propose(id="review", name="Review", role="reviewer"), **usage)
        if done < self.lead_views:
            return response(("lead__view", {}), **usage)
        if self.lead_serial and done <= self.lead_views + 2:
            # One valid proposal per response: view, three proposals, then the final answer.
            serial = [
                propose(id="code", name="Fix add()", role="coder"),
                propose(id="test", name="Run the tests", role="tester", dependencies=["code"]),
                propose(id="plan2", name="Plan the review", role="lead2"),
            ]
            return response(serial[done - self.lead_views], **usage)
        if done == self.lead_views:
            return response(
                propose(id="code", name="Fix add()", role="coder"),
                propose(id="test", name="Run the tests", role="tester", dependencies=["code"]),
                propose(id="plan2", name="Plan the review", role="lead2"),
                **usage,
            )
        return response(text="planned", **usage)


def config(**changes: Any) -> lt.TeamConfig:
    values: dict[str, Any] = {
        "live": True,
        "limits": _limits(),
        "pricing": PRICES,
        "scenarios": ("LIVE_ACCEPTED",),
        "authorization": lt.Authorization("gpt-5.4-mini", 0.5, ("LIVE_ACCEPTED",)),
    }
    values.update(changes)
    if "scenarios" in changes and "authorization" not in changes:
        values["authorization"] = lt.Authorization("gpt-5.4-mini", 0.5, tuple(changes["scenarios"]))
    return lt.TeamConfig(**values)


def run(
    cfg: lt.TeamConfig, client: Any, sandbox: Any = standin, environ: Any = None
) -> lt.TeamReport:
    return asyncio.run(
        lt.run_team(cfg, client=client, sandbox_factory=sandbox, environ=environ or {})
    )


# --------------------------------------------------------------------------- offline plan


def test_the_plan_covers_the_exact_three_scenarios_and_is_ready_with_known_prices() -> None:
    result = lt.plan(config(live=False, scenarios=lt.ALL_SCENARIOS))
    assert result["verdict"] == "READY_FOR_AUTHORIZATION_REQUEST" and result["executed"] is False
    assert result["scenarios"] == list(lt.ALL_SCENARIOS)
    assert result["expected_requests"] == 27 and result["strict_schema_issues"] == []
    assert result["tools"]["testing__run"] is False and result["tools"]["review__submit"] is True


def test_the_recommended_defaults_form_a_ready_plan_for_the_accepted_scenario() -> None:
    result = lt.plan(lt.TeamConfig(pricing=PRICES))  # every default limit, no overrides
    assert result["verdict"] == "READY_FOR_AUTHORIZATION_REQUEST", result["blockers"]
    assert result["scenarios"] == ["LIVE_ACCEPTED"]
    assert (result["expected_requests"], result["maximum_valid_requests"]) == (9, 11)
    assert result["limits"]["max_requests"] == 12 and result["limits"]["max_steps"] == 6
    assert result["worst_case_cost_usd"] == pytest.approx(0.135)
    assert result["worst_case_cost_usd"] <= result["limits"]["budget_usd"] == 0.15


def test_the_plan_is_not_ready_without_prices_or_with_too_few_requests() -> None:
    unpriced = lt.plan(config(live=False, pricing=lv.Pricing()))
    assert unpriced["verdict"] == "NOT_READY"
    few = lt.plan(config(live=False, scenarios=lt.ALL_SCENARIOS, limits=_limits(max_requests=20)))
    assert few["verdict"] == "NOT_READY" and any("below the 33" in b for b in few["blockers"])


def _limits(**changes: Any) -> lv.Limits:
    values: dict[str, Any] = {
        "max_requests": 34,
        "max_steps": 4,
        "max_tool_calls": 10,
        "max_output_tokens": 1500,
        "max_input_tokens": 6_000,
        "request_timeout": 90.0,
        "total_timeout": 900.0,
        "budget_usd": 0.5,
    }
    values.update(changes)
    return lv.Limits(**values)


@pytest.mark.parametrize(
    "limits",
    [
        {"budget_usd": float("nan")},
        {"budget_usd": float("inf")},
        {"total_timeout": float("nan")},
        {"request_timeout": float("inf")},
        {"max_steps": 9},
    ],
)
def test_invalid_limits_never_make_a_ready_plan_or_a_run(limits: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        lt.plan(config(live=False, limits=_limits(**limits)))
    with pytest.raises(ValueError):
        run(config(limits=_limits(**limits)), FakeTeamOpenAI())


@pytest.mark.parametrize("price", ["nan", "inf", "-1", "0"])
def test_an_invalid_price_is_a_configuration_error_and_no_report_is_written(
    price: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("nothing may start")

    monkeypatch.setattr(lv, "_real_client", forbidden)
    monkeypatch.setattr(asyncio, "run", forbidden)
    report = tmp_path / "r.json"
    for flag in ("--price-input-per-mtok", "--price-output-per-mtok"):
        arguments = ["plan", f"{flag}={price}", "--report", str(report)]
        assert lt.main(arguments) == 2
    assert not report.exists() and "READY" not in capsys.readouterr().out


def test_the_plan_command_makes_no_call_and_never_prints_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setattr(lv, "_real_client", lambda *a, **k: pytest.fail("a client was created"))
    report = tmp_path / "plan.json"
    arguments = ["plan", "--price-input-per-mtok=0.75", "--price-output-per-mtok=4.5"]
    assert lt.main([*arguments, "--report", str(report)]) == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err + report.read_text()
    assert lt.main(["plan"]) == 1  # unknown prices: not ready


# ------------------------------------------------------------------ authorization is required


def test_a_dry_run_sends_nothing_and_is_not_a_pass() -> None:
    client = FakeTeamOpenAI()
    report = run(config(live=False), client)
    assert report.verdict == "NOT_RUN" and report.scenarios == [] and client.requests == []


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"authorization": None}, "not authorized"),
        ({"authorization": lt.Authorization("other-model", 0.5, ("LIVE_ACCEPTED",))}, "model"),
        ({"authorization": lt.Authorization("gpt-5.4-mini", 0.5, ("LIVE_FAILING_TESTS",))}, "scen"),
        ({"authorization": lt.Authorization("gpt-5.4-mini", 0.1, ("LIVE_ACCEPTED",))}, "budget"),
        (
            {"authorization": lt.Authorization("gpt-5.4-mini", float("nan"), ("LIVE_ACCEPTED",))},
            "finite",
        ),
        ({"pricing": lv.Pricing()}, "not ready"),
    ],
    ids=["none", "model", "scenarios", "spend", "nan_spend", "unpriced"],
)
def test_a_live_run_without_an_exact_authorization_sends_nothing(
    changes: dict[str, Any], why: str
) -> None:
    client = FakeTeamOpenAI()
    report = run(config(**changes), client)
    assert report.failure is not None and why in report.failure
    assert report.verdict == "FAIL" and report.model_requests == 0 and client.requests == []
    assert report.billing_note == "no request was sent"


# --------------------------------------------------------------- real composition, fake model


def test_the_accepted_scenario_completes_through_the_real_stack_with_nine_requests() -> None:
    client = FakeTeamOpenAI()
    report = run(config(), client)
    item = report.scenarios[0]
    assert report.verdict == "PASS", (item.verdict, item.reason)
    assert (item.outcome_status, item.run_status) == ("completed", "completed")
    assert item.team_acceptance is not None and item.team_acceptance.startswith("accepted")
    assert item.evidence_commands == ["unit.tests"]
    # Four model-driven roles, nine requests, all through the one shared ledger.
    assert {role: usage.requests for role, usage in item.roles.items()} == {
        "lead": 3,
        "lead2": 2,
        "tester": 2,
        "reviewer": 2,
    }
    assert report.model_requests == len(client.requests) == 9
    assert report.estimated_cost_usd is not None and report.estimated_cost_usd > 0


def test_every_request_carries_the_registered_role_prompt_a_brief_and_strict_tool_schemas() -> None:
    client = FakeTeamOpenAI()
    run(config(), client)
    assert len(client.requests) == 9
    for request in client.requests:
        assert request["store"] is False and request["max_output_tokens"] == 1500
        assert "VALIDATION BRIEF" in request["instructions"]
        for tool in request["tools"]:
            if tool["name"] != "testing__run":
                assert tool["strict"] is True, tool["name"]
    # The model-visible Tester tool carries no command line, path, image or permission field.
    tester = next(r for r in client.requests if r["tools"][0]["name"] == "testing__run")
    assert set(tester["tools"][0]["parameters"]["properties"]) == {"command_id", "parameters"}
    blob = json.dumps([r["tools"] for r in client.requests])
    assert "image" not in blob and "docker" not in blob and "workspace" not in blob


def test_failing_tests_are_blocked_whatever_the_model_claims() -> None:
    client = FakeTeamOpenAI()  # the tester's closing text claims success
    report = run(config(scenarios=("LIVE_FAILING_TESTS",)), client)
    item = report.scenarios[0]
    assert report.verdict == "PASS", (item.verdict, item.reason)
    assert item.outcome_status == "blocked" and item.outcome_reason == "quality_blocked"
    assert item.run_status != "completed" and item.evidence_commands == ["unit.tests"]


def test_unrelated_evidence_never_satisfies_acceptance() -> None:
    client = FakeTeamOpenAI()
    report = run(config(scenarios=("LIVE_INVALID_EVIDENCE",)), client)
    item = report.scenarios[0]
    assert item.outcome_status != "completed" and item.run_status != "completed"
    assert item.evidence_commands == ["compile.check"]
    assert report.verdict == "PASS", (item.verdict, item.reason)


@pytest.mark.parametrize("reviewer", ["fabricated", "none"])
def test_a_reviewer_citing_missing_or_invented_evidence_cannot_complete_the_delivery(
    reviewer: str,
) -> None:
    report = run(config(), FakeTeamOpenAI(reviewer=reviewer))
    item = report.scenarios[0]
    assert item.run_status != "completed" and item.outcome_status != "completed"
    assert report.verdict != "PASS"


@pytest.mark.parametrize("tester", ["unauthorized_tool", "unregistered_command"])
def test_an_unauthorized_tool_or_command_runs_nothing_and_yields_no_evidence(
    tester: str,
) -> None:
    report = run(config(), FakeTeamOpenAI(tester=tester))
    item = report.scenarios[0]
    assert item.evidence_commands == [] and item.run_status != "completed"
    assert item.outcome_status == "failed" and report.verdict != "PASS"


def test_a_failed_or_incomplete_run_is_never_reported_as_delivered() -> None:
    report = run(config(scenarios=lt.ALL_SCENARIOS), FakeTeamOpenAI(tester="unauthorized_tool"))
    assert all(item.run_status != "completed" for item in report.scenarios)
    assert report.verdict in {"FAIL", "INCONCLUSIVE"}


# ----------------------------------------------------------------------- guards and failures


def test_the_request_ceiling_stops_the_run_before_the_next_request_is_sent() -> None:
    # A Lead that looks four times needs a twelfth request; the plan allows eleven.
    client = FakeTeamOpenAI(lead_views=4)
    report = run(config(limits=_limits(max_requests=11, max_steps=6)), client)
    assert report.abort_reason == "request limit reached" and len(client.requests) == 11
    assert report.verdict == "INCONCLUSIVE" and report.scenarios[0].run_status != "completed"


def test_the_budget_guard_stops_a_run_whose_provider_reports_more_usage_than_planned() -> None:
    # The plan fits the budget (nine requests at the limits), but this provider reports far more
    # input tokens than the limits allow, so the running total must stop the run, not the plan.
    client = FakeTeamOpenAI(tokens=(60_000, 1_500))
    report = run(config(limits=_limits(max_requests=11, budget_usd=0.18)), client)
    assert report.abort_reason in {"budget estimate exceeded", "budget exceeded after the request"}
    assert len(client.requests) < 9 and report.verdict != "PASS"
    assert report.scenarios[0].run_status != "completed"


def test_a_plan_whose_worst_case_exceeds_the_budget_is_refused_before_any_request() -> None:
    client = FakeTeamOpenAI()
    report = run(config(limits=_limits(budget_usd=0.0001)), client)
    assert report.failure is not None and "exceeds the budget" in report.failure
    assert client.requests == [] and report.model_requests == 0


def test_the_input_size_guard_stops_oversized_requests() -> None:
    client = FakeTeamOpenAI()
    report = run(config(limits=_limits(max_input_tokens=200)), client)
    assert report.abort_reason == "input token limit exceeded" and client.requests == []


def test_the_tool_call_ceiling_aborts_the_run() -> None:
    client = FakeTeamOpenAI()
    report = run(config(limits=_limits(max_tool_calls=2)), client)
    assert report.abort_reason == "tool-call limit exceeded" and report.verdict != "PASS"


def test_a_plan_below_the_request_ceiling_is_refused_before_any_request() -> None:
    client = FakeTeamOpenAI()
    report = run(config(limits=_limits(max_requests=3)), client)
    assert report.failure is not None and "below the 11" in report.failure
    assert client.requests == []


def test_the_step_limit_bounds_every_task_and_a_starved_run_does_not_complete() -> None:
    client = FakeTeamOpenAI()
    report = run(config(limits=_limits(max_steps=1)), client)
    assert report.verdict != "PASS" and report.scenarios[0].run_status != "completed"


def test_a_run_spends_less_than_the_worst_case_and_stays_under_the_budget() -> None:
    client = FakeTeamOpenAI()
    cfg = config(scenarios=lt.ALL_SCENARIOS)
    report = run(cfg, client)
    assert report.estimated_cost_usd is not None
    assert report.estimated_cost_usd <= (report.worst_case_cost_usd or 0) <= cfg.limits.budget_usd


def test_there_are_no_automatic_retries_a_provider_failure_ends_the_run() -> None:
    class Flaky(FakeTeamOpenAI):
        async def _create(self, **request: Any) -> Any:
            self.requests.append(request)
            error = RuntimeError("provider exploded " + SECRET)
            error.status_code = 500  # type: ignore[attr-defined]
            raise error

    client = Flaky()
    report = run(config(), client, environ={"OPENAI_API_KEY": SECRET})
    assert len(client.requests) == 1 and report.model_requests == 1  # one attempt, no retry
    assert report.verdict != "PASS" and report.provider_error is not None
    assert report.provider_error["http_status"] == 500
    assert SECRET not in lt.render(report, {"OPENAI_API_KEY": SECRET})
    assert "exploded" not in lt.render(report, {"OPENAI_API_KEY": SECRET})


def test_a_missing_model_or_failed_authentication_is_reported_with_no_model_request() -> None:
    class Missing(FakeTeamOpenAI):
        async def _retrieve(self, model: str) -> Any:
            error = LookupError("no such model")
            error.status_code = 404  # type: ignore[attr-defined]
            raise error

    client = Missing()
    report = run(config(), client)
    assert report.failure == "provider unavailable: not_found"
    assert client.requests == [] and report.model_requests == 0 and report.verdict == "FAIL"


def test_the_overall_timeout_ends_the_run_without_completing_anything() -> None:
    class Hangs(FakeTeamOpenAI):
        async def _create(self, **request: Any) -> Any:
            self.requests.append(request)
            await asyncio.Event().wait()  # never answers

    client = Hangs()
    report = run(config(limits=_limits(total_timeout=0.5)), client)
    assert report.failure == "overall timeout reached" and report.verdict != "PASS"
    assert not any(item.run_status == "completed" for item in report.scenarios)


def test_cancellation_propagates_and_stops_the_run() -> None:
    entered = asyncio.Event
    state: dict[str, Any] = {}

    class Blocks(FakeTeamOpenAI):
        async def _create(self, **request: Any) -> Any:
            self.requests.append(request)
            state["entered"].set()
            await asyncio.Event().wait()

    async def scenario() -> None:
        state["entered"] = entered()
        client = Blocks()
        task = asyncio.ensure_future(
            lt.run_team(config(), client=client, sandbox_factory=standin, environ={})
        )
        await state["entered"].wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(client.requests) == 1  # nothing further was sent

    asyncio.run(scenario())


def test_an_unavailable_sandbox_sends_no_model_request_and_has_no_local_fallback() -> None:
    from kaiwen_agent.toolkits import SandboxUnavailableError

    def unavailable(filesystem: Any) -> Any:
        raise SandboxUnavailableError("no engine")

    client = FakeTeamOpenAI()
    report = run(config(), client, sandbox=unavailable)
    assert report.docker == "unavailable" and client.requests == []
    assert report.scenarios[0].verdict == "INCONCLUSIVE" and report.verdict != "PASS"


def test_a_missing_docker_engine_fails_closed_before_any_model_request(tmp_path: Path) -> None:
    """No sandbox factory: the pinned runner is built and verified; no local fallback."""

    cfg = config(docker_path=str(tmp_path / "no-such-docker"))
    client = FakeTeamOpenAI()
    report = asyncio.run(lt.run_team(cfg, client=client, environ={}))
    assert report.docker == "unavailable" and client.requests == [] and report.verdict != "PASS"
    assert report.model_requests == 0 and report.scenarios[0].evidence_commands == []


def test_a_secret_in_the_environment_never_reaches_the_report() -> None:
    client = FakeTeamOpenAI()
    report = run(config(), client, environ={"OPENAI_API_KEY": SECRET})
    assert SECRET not in lt.render(report, {"OPENAI_API_KEY": SECRET})


# ------------------------------------------------------------------------ the judge, directly


def verdict_of(
    name: str,
    *,
    status: Any,
    run_status: str,
    reason: str | None = None,
    acceptance: str = "accepted:ok",
    evidence: tuple[str, ...] = ("unit.tests",),
    aborted: str | None = None,
) -> str:
    result = SimpleNamespace(
        outcome=SimpleNamespace(status=status, reason=reason),
        run_status=run_status,
        acceptance=acceptance,
    )
    ledger = lv.Ledger(_limits(), PRICES)
    ledger.abort_reason = aborted
    return lt.judge(lt.LIVE_SCENARIOS[name], result, list(evidence), ledger)[0]


STATUS = lt.WorkflowOutcomeStatus


def test_the_judge_never_passes_a_completed_negative_scenario() -> None:
    for name in ("LIVE_FAILING_TESTS", "LIVE_INVALID_EVIDENCE"):
        assert verdict_of(name, status=STATUS.COMPLETED, run_status="completed") == "FAIL"


def test_the_judge_fails_an_outcome_that_disagrees_with_the_durable_run_status() -> None:
    assert verdict_of("LIVE_ACCEPTED", status=STATUS.COMPLETED, run_status="running") == "FAIL"
    assert verdict_of("LIVE_FAILING_TESTS", status=STATUS.BLOCKED, run_status="completed") == "FAIL"


def test_the_judge_needs_gate_verified_evidence_and_an_accepting_decision_for_a_pass() -> None:
    accepted = {"status": STATUS.COMPLETED, "run_status": "completed"}
    assert verdict_of("LIVE_ACCEPTED", **accepted) == "PASS"
    assert verdict_of("LIVE_ACCEPTED", evidence=(), **accepted) == "FAIL"
    assert verdict_of("LIVE_ACCEPTED", acceptance="blocked:x", **accepted) == "FAIL"


def test_the_judge_treats_an_aborted_run_as_inconclusive_never_as_a_pass() -> None:
    blocked = {"status": STATUS.BLOCKED, "run_status": "blocked", "reason": "quality_blocked"}
    assert verdict_of("LIVE_FAILING_TESTS", **blocked) == "PASS"
    assert verdict_of("LIVE_FAILING_TESTS", aborted="request limit reached", **blocked) == (
        "INCONCLUSIVE"
    )


def test_a_negative_scenario_where_nothing_was_tested_is_inconclusive_not_a_pass() -> None:
    blocked = {"status": STATUS.BLOCKED, "run_status": "blocked", "reason": "quality_blocked"}
    assert verdict_of("LIVE_FAILING_TESTS", evidence=(), **blocked) == "INCONCLUSIVE"
    assert verdict_of("LIVE_FAILING_TESTS", **{**blocked, "reason": "other"}) == "INCONCLUSIVE"


def test_after_an_abort_no_further_scenario_is_started() -> None:
    client = FakeTeamOpenAI(tokens=(60_000, 1_500))
    limits = _limits(max_requests=33, budget_usd=0.45)
    report = run(config(scenarios=lt.ALL_SCENARIOS, limits=limits), client)
    assert report.abort_reason is not None and len(report.scenarios) == 1
    assert len(client.requests) < 27


def test_the_rendered_report_scrubs_a_configured_secret_wherever_it_appears() -> None:
    text = lt.render({"note": f"leaked {SECRET} here"}, {"OPENAI_API_KEY": SECRET})
    assert SECRET not in text and "[REDACTED]" in text


def test_the_real_client_is_built_with_the_request_timeout_and_no_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one real client: SDK retries off (every request is counted) and the timeout set."""

    import openai

    seen: dict[str, Any] = {}

    class Recorder:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(openai, "AsyncOpenAI", Recorder)
    provider_config = lv.Config(provider="openai", model="m", live=True, limits=_limits())
    lv._real_client(provider_config, {"OPENAI_API_KEY": SECRET})
    assert seen["max_retries"] == 0 and seen["timeout"] == 90.0


# ------------------------------------------------------------------ endpoint integrity (R3)

CANONICAL = "https://api.openai.com/v1"
REDIRECT = "http://127.0.0.1:9/v1"  # never contacted: every transport below is mocked


class WireRecorder:
    """Replaces the HTTP transport of every SDK client: records where a request would go."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        self.sent: list[Any] = []

        async def send(client: Any, request: Any, **kwargs: Any) -> Any:
            self.sent.append(request)
            body = {"id": "gpt-5.4-mini", "object": "model", "created": 0, "owned_by": "x"}
            return httpx.Response(200, json=body, request=request)

        monkeypatch.setattr(httpx.AsyncClient, "send", send)

    @property
    def hosts(self) -> set[str]:
        return {request.url.host for request in self.sent}


@pytest.mark.parametrize("override", [None, CANONICAL, CANONICAL + "/"])
def test_preflight_sends_the_credential_only_to_the_official_endpoint(
    monkeypatch: pytest.MonkeyPatch, override: str | None
) -> None:
    if override is None:
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("OPENAI_BASE_URL", override)
    wire = WireRecorder(monkeypatch)
    result = asyncio.run(lt.preflight("gpt-5.4-mini", **keyed()))
    assert result["status"] == "OK" and wire.hosts == {"api.openai.com"}
    assert wire.sent[0].headers["authorization"] == f"Bearer {SECRET}"  # only to that host


@pytest.mark.parametrize(
    "override", [REDIRECT, "https://evil.example/v1", "https://api.openai.com.evil.test/v1"]
)
def test_a_redirecting_base_url_in_the_process_environment_never_receives_the_credential(
    monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", override)
    wire = WireRecorder(monkeypatch)
    result = asyncio.run(lt.preflight("gpt-5.4-mini", **keyed()))
    assert result["status"] == "ENDPOINT_REJECTED" and result["model_requests"] == 0
    assert wire.sent == []  # nothing, to any host, carried the key
    assert override not in json.dumps(result) and SECRET not in json.dumps(result)


def test_a_redirecting_base_url_from_the_loaded_configuration_is_refused_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The `.env` the harness loads may carry it even when the process environment does not."""

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join([f"OPENAI_API_KEY={SECRET}", f"OPENAI_BASE_URL={REDIRECT}", ""]))
    wire = WireRecorder(monkeypatch)
    result = asyncio.run(lt.preflight("gpt-5.4-mini", environ={}, env_file=env_file))
    assert result["status"] == "ENDPOINT_REJECTED" and wire.sent == []


@pytest.mark.parametrize("override", [None, REDIRECT])
def test_the_paid_execution_client_is_pinned_to_the_official_endpoint_or_refused(
    monkeypatch: pytest.MonkeyPatch, override: str | None
) -> None:
    if override is None:
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("OPENAI_BASE_URL", override)
    provider_config = lv.Config(provider="openai", model="m", live=True, limits=_limits())
    if override is not None:
        with pytest.raises(lv.HarnessError, match="OPENAI_BASE_URL"):
            lv._real_client(provider_config, {"OPENAI_API_KEY": SECRET})
        return
    client = lv._real_client(provider_config, {"OPENAI_API_KEY": SECRET})
    assert str(client.base_url).rstrip("/") == CANONICAL


def test_the_endpoint_is_passed_explicitly_so_the_sdk_never_consults_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    seen: dict[str, Any] = {}

    class Recorder:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(openai, "AsyncOpenAI", Recorder)
    lv.openai_client(SECRET, timeout=5.0, environment={})
    assert seen["base_url"] == CANONICAL and seen["max_retries"] == 0


def test_a_redirect_in_the_configuration_mapping_is_refused_before_a_paid_run_sends_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    wire = WireRecorder(monkeypatch)
    environ = {"OPENAI_API_KEY": SECRET, "OPENAI_BASE_URL": REDIRECT}
    report = asyncio.run(lt.run_team(config(), sandbox_factory=standin, environ=environ))
    assert report.failure is not None and "OPENAI_BASE_URL" in report.failure
    assert wire.sent == [] and report.model_requests == 0 and report.verdict != "PASS"


def test_an_injected_client_is_untouched_by_the_endpoint_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline fakes keep working whatever the environment says: no credential can leave."""

    monkeypatch.setenv("OPENAI_BASE_URL", REDIRECT)
    report = run(config(), FakeTeamOpenAI())
    assert report.verdict == "PASS"
    result = asyncio.run(lt.preflight("m", **keyed(client=FakeTeamOpenAI())))
    assert result["status"] == "OK"


# ---------------------------------------------------------------------------------- preflight


def keyed(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"environ": {"OPENAI_API_KEY": SECRET}, "env_file": None}
    values.update(changes)
    return values


def test_preflight_without_a_key_reports_key_not_found_and_calls_nothing(tmp_path: Path) -> None:
    class Client:
        models = SimpleNamespace(retrieve=lambda m: pytest.fail("a request was made"))

    result = asyncio.run(
        lt.preflight("m", environ={}, env_file=tmp_path / "missing.env", client=Client())
    )
    assert result["status"] == "KEY_NOT_FOUND" and result["balance"] == "BALANCE_UNVERIFIED"
    assert result["model_requests"] == 0


def test_preflight_reads_only_the_named_env_file_and_never_prints_the_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"OPENAI_API_KEY={SECRET}\n")
    client = FakeTeamOpenAI()
    result = asyncio.run(lt.preflight("m", environ={}, env_file=env_file, client=client))
    assert result["status"] == "OK" and result["key_source"] == "env_file"
    assert result["tool_calling"] == "not_verified" and result["balance"] == "BALANCE_UNVERIFIED"
    assert SECRET not in lt.render(result, {"OPENAI_API_KEY": SECRET})
    assert client.requests == []  # a model listing is not a generation


@pytest.mark.parametrize(
    "key", ["short", "sk-has space in it 0123456789", " sk-leading0123456789012345"]
)
def test_a_malformed_key_is_rejected_before_any_request(key: str) -> None:
    class Client:
        models = SimpleNamespace(retrieve=lambda m: pytest.fail("a request was made"))

    result = asyncio.run(
        lt.preflight("m", environ={"OPENAI_API_KEY": key}, env_file=None, client=Client())
    )
    assert result["status"] == "KEY_MALFORMED"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (type("AuthErr", (Exception,), {"status_code": 401}), "AUTHENTICATION_FAILED"),
        (type("PermErr", (Exception,), {"status_code": 403}), "PERMISSION_DENIED"),
        (type("NotFound", (Exception,), {"status_code": 404}), "MODEL_UNAVAILABLE"),
        (
            type("Quota", (Exception,), {"status_code": 429, "code": "insufficient_quota"}),
            "QUOTA_EXHAUSTED",
        ),
        (type("Rate", (Exception,), {"status_code": 429}), "RATE_LIMITED"),
        (type("Boom", (Exception,), {"status_code": 503}), "PROVIDER_ERROR"),
        (type("APIConnectionError", (Exception,), {}), "NETWORK_FAILURE"),
        (TimeoutError, "NETWORK_FAILURE"),
    ],
)
def test_preflight_distinguishes_authentication_model_network_and_provider_failures(
    error: type[Exception], status: str
) -> None:
    class Client:
        async def _retrieve(self, model: str) -> Any:
            raise error("body with " + SECRET)

        def __init__(self) -> None:
            self.models = SimpleNamespace(retrieve=self._retrieve)

    result = asyncio.run(lt.preflight("m", **keyed(client=Client())))
    assert result["status"] == status and result["model_requests"] == 0
    assert SECRET not in json.dumps(result) and "body with" not in json.dumps(result)


# -------------------------------------------------------------------------------- command line


def test_the_run_command_is_a_dry_run_and_refuses_an_unauthorized_live_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setattr(lv, "_real_client", lambda *a, **k: pytest.fail("a client was created"))
    prices = ["--price-input-per-mtok=0.75", "--price-output-per-mtok=4.5"]
    env = ["--env-file", str(tmp_path / "none.env")]
    assert lt.main(["run", *prices, *env]) == 0
    assert '"verdict": "NOT_RUN"' in capsys.readouterr().out
    assert lt.main(["run", "--live", *prices, *env]) == 1  # refused: no authorization
    output = capsys.readouterr().out
    assert "paid execution is not authorized" in output and SECRET not in output
    assert lt.main(["run", "--live", "--authorize-head=" + "0" * 40, *prices, *env]) == 2


# ----------------------------------------------------------------------- real Docker (Linux CI)


@needs_docker
def test_the_accepted_delivery_runs_its_tests_in_a_real_container() -> None:
    cfg = config(image=TEST_IMAGE)
    client = FakeTeamOpenAI()
    report = asyncio.run(lt.run_team(cfg, client=client, environ={}))
    item = report.scenarios[0]
    assert report.docker == "available" and report.verdict == "PASS", (item.verdict, item.reason)
    assert item.evidence_commands == ["unit.tests"] and item.run_status == "completed"


@needs_docker
def test_failing_tests_and_unrelated_evidence_are_blocked_with_a_real_container() -> None:
    names = ("LIVE_FAILING_TESTS", "LIVE_INVALID_EVIDENCE")
    cfg = config(image=TEST_IMAGE, scenarios=names)
    report = asyncio.run(lt.run_team(cfg, client=FakeTeamOpenAI(), environ={}))
    assert [item.run_status != "completed" for item in report.scenarios] == [True, True]
    assert report.verdict == "PASS", [(i.verdict, i.reason) for i in report.scenarios]
