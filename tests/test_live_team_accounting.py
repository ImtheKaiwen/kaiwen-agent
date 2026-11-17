"""M6.4-C R4: stopping, per-role attribution and in-flight budget accounting of the live harness.

The first authorized live run showed three harness defects, reproduced here without any network:
the run continued to the next scenario after an INCONCLUSIVE one, the per-role request counters of
two roles that run at the same time did not add up to the run total (nine real requests, eleven
attributed), and the ledger kept a single shared "pending cost", so overlapping requests could
overwrite each other's reservation and be admitted without counting each other. Deterministic
barriers and `asyncio.sleep(0)` interleaving stand in for the network; a deterministic fake provider
passing proves nothing about a real provider.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import live_team_validation as lt
from scripts import live_validation as lv
from tests.test_live_team_validation import (
    PRICES,
    SECRET,
    FakeTeamOpenAI,
    _limits,
    config,
    run,
)

# ----------------------------------------------------------- a bare ledger with overlapping calls


def request(size: int = 2900) -> dict[str, Any]:
    return {"model": "m", "input": "x" * size}


def one_request_cost(ledger_limits: lv.Limits, size: int = 2900) -> float:
    prepared = {
        **request(size),
        "store": False,
        "max_output_tokens": ledger_limits.max_output_tokens,
    }
    return PRICES.cost(lv.estimate_tokens(prepared), ledger_limits.max_output_tokens)


def reply(tokens: tuple[int, int] | None = (1000, 20)) -> Any:
    usage = (
        None if tokens is None else SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1])
    )
    return SimpleNamespace(id="r", output=[], usage=usage)


class Inner:
    """A provider whose calls wait for the test to release them, one by one."""

    def __init__(self) -> None:
        self.started: list[asyncio.Event] = []
        self.release: list[asyncio.Event] = []
        self.outcomes: list[Any] = []

    def script(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.started = [asyncio.Event() for _ in outcomes]
        self.release = [asyncio.Event() for _ in outcomes]
        self.calls = 0
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        index = self.calls
        self.calls += 1
        self.started[index].set()
        await self.release[index].wait()
        outcome = self.outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_overlapping_requests_cannot_overspend_the_budget() -> None:
    limits = _limits(max_requests=10)
    single = one_request_cost(limits)
    limits = _limits(max_requests=10, budget_usd=single * 1.5)  # room for one, never for two

    async def scenario() -> tuple[Inner, BaseException | None]:
        ledger = lv.Ledger(limits, PRICES)
        inner = Inner()
        inner.script(reply())
        client = lv.GuardedOpenAIClient(inner, ledger)
        first = asyncio.ensure_future(client.responses.create(**request()))
        await inner.started[0].wait()  # the first request is in flight
        refusal: BaseException | None = None
        try:
            await client.responses.create(**request())  # admitted only if the budget really fits
        except lv.LimitExceeded as error:
            refusal = error
        inner.release[0].set()
        await first
        return inner, refusal

    inner, refusal = asyncio.run(scenario())
    assert refusal is not None and "budget" in str(refusal)
    assert inner.calls == 1  # the second request was never sent


def test_a_failing_request_is_charged_its_own_estimate_not_another_requests() -> None:
    limits = _limits(max_requests=10, budget_usd=5.0)
    big, small = one_request_cost(limits, 2900), one_request_cost(limits, 300)
    assert big > small

    async def scenario() -> float:
        ledger = lv.Ledger(limits, PRICES)
        inner = Inner()
        failure = RuntimeError("boom")
        failure.status_code = 500  # type: ignore[attr-defined]
        inner.script(failure, reply())
        client = lv.GuardedOpenAIClient(inner, ledger)
        first = asyncio.ensure_future(client.responses.create(**request(2900)))
        await inner.started[0].wait()
        second = asyncio.ensure_future(client.responses.create(**request(300)))
        await inner.started[1].wait()  # both in flight; the second reservation is the newest
        inner.release[0].set()
        with pytest.raises(RuntimeError):
            await first
        spent_after_failure = ledger.spent_usd
        inner.release[1].set()
        await second
        return spent_after_failure

    assert asyncio.run(scenario()) == pytest.approx(big)


def test_a_cancelled_request_is_counted_as_unknown_billing_and_releases_its_reservation() -> None:
    limits = _limits(max_requests=10, budget_usd=5.0)
    single = one_request_cost(limits)

    async def scenario() -> lv.Ledger:
        ledger = lv.Ledger(limits, PRICES)
        inner = Inner()
        inner.script(reply())
        client = lv.GuardedOpenAIClient(inner, ledger)
        task = asyncio.ensure_future(client.responses.create(**request()))
        await inner.started[0].wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return ledger

    ledger = asyncio.run(scenario())
    assert ledger.spent_usd == pytest.approx(single)  # it may have been billed: never zero
    assert ledger.reserved_usd == 0.0  # and nothing stays reserved
    assert ledger.errors and ledger.errors[0].category == "cancelled"


def test_a_ledger_that_stopped_refuses_every_later_request() -> None:
    ledger = lv.Ledger(_limits(max_requests=1), PRICES)
    ledger.before(100)
    with pytest.raises(lv.LimitExceeded):
        ledger.before(100)  # the ceiling: this aborts the run
    assert ledger.abort_reason == "request limit reached"
    ledger.requests = 0  # even if a counter were reset, the run has stopped
    with pytest.raises(lv.LimitExceeded, match="request limit reached"):
        ledger.before(100)


def test_a_failed_request_stops_every_later_request() -> None:
    ledger = lv.Ledger(_limits(), PRICES)
    reservation = ledger.before(100)
    ledger.record_failure(RuntimeError("x"), reservation)
    with pytest.raises(lv.LimitExceeded, match="failed request"):
        ledger.before(100)


def overlapped(ledger: lv.Ledger, inner: Inner, *outcomes: Any) -> tuple[Any, Any]:
    """Two roles, one request each, both in flight at the same time, then released together."""

    inner.script(*outcomes)
    return (
        lv.GuardedOpenAIClient(inner, ledger, "lead2"),
        lv.GuardedOpenAIClient(inner, ledger, "tester"),
    )


async def run_overlapped(ledger: lv.Ledger, outcomes: tuple[Any, Any]) -> list[Any]:
    inner = Inner()
    first, second = overlapped(ledger, inner, *outcomes)
    a = asyncio.ensure_future(first.responses.create(**request()))
    await inner.started[0].wait()
    b = asyncio.ensure_future(second.responses.create(**request()))
    await inner.started[1].wait()  # both are in flight: the windows overlap
    inner.release[0].set()
    inner.release[1].set()
    return await asyncio.gather(a, b, return_exceptions=True)


def test_overlapping_roles_are_each_attributed_exactly_their_own_request_and_usage() -> None:
    ledger = lv.Ledger(_limits(max_requests=10, budget_usd=5.0), PRICES)
    asyncio.run(run_overlapped(ledger, (reply((1000, 20)), reply((700, 10)))))
    lead2, tester = ledger.roles["lead2"], ledger.roles["tester"]
    assert (lead2.requests, tester.requests, ledger.requests) == (1, 1, 2)
    assert (lead2.input_tokens, lead2.output_tokens) == (1000, 20)
    assert (tester.input_tokens, tester.output_tokens) == (700, 10)
    assert ledger.input_tokens == 1700 and ledger.reserved_usd == 0.0


def test_an_overlapping_failure_belongs_to_one_role_and_the_other_keeps_its_usage() -> None:
    limits = _limits(max_requests=10, budget_usd=5.0)
    failure = RuntimeError("boom")
    failure.status_code = 500  # type: ignore[attr-defined]
    ledger = lv.Ledger(limits, PRICES)
    results = asyncio.run(run_overlapped(ledger, (failure, reply((700, 10)))))
    assert isinstance(results[0], RuntimeError)
    lead2, tester = ledger.roles["lead2"], ledger.roles["tester"]
    assert (lead2.failed, tester.failed) == (1, 0)
    assert (lead2.usage_responses, tester.usage_responses) == (0, 1)
    assert ledger.spent_usd == pytest.approx(one_request_cost(limits) + PRICES.cost(700, 10))
    assert ledger.guard_failures == 1 and ledger.reserved_usd == 0.0


def test_an_overlapping_response_without_usage_is_charged_at_its_estimate_for_its_role() -> None:
    limits = _limits(max_requests=10, budget_usd=5.0)
    ledger = lv.Ledger(limits, PRICES)
    asyncio.run(run_overlapped(ledger, (reply((1000, 20)), reply(None))))
    lead2, tester = ledger.roles["lead2"], ledger.roles["tester"]
    assert (lead2.usage_responses, tester.usage_responses) == (1, 0)
    assert tester.responses == 1 and tester.input_tokens == 0  # not invented
    assert ledger.spent_usd == pytest.approx(PRICES.cost(1000, 20) + one_request_cost(limits))


def test_a_timed_out_request_is_cancelled_charged_and_released() -> None:
    limits = _limits(max_requests=10, budget_usd=5.0)
    ledger = lv.Ledger(limits, PRICES)

    async def scenario() -> None:
        inner = Inner()
        inner.script(reply())
        client = lv.GuardedOpenAIClient(inner, ledger, "tester")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.responses.create(**request()), timeout=0.05)

    asyncio.run(scenario())
    assert ledger.roles["tester"].failed == 1 and ledger.reserved_usd == 0.0
    assert ledger.spent_usd == pytest.approx(one_request_cost(limits))
    assert ledger.errors[0].category == "cancelled"


def test_the_in_flight_budget_holds_across_roles() -> None:
    limits = _limits(max_requests=10)
    single = one_request_cost(limits)
    limits = _limits(max_requests=10, budget_usd=single * 1.5)

    async def scenario() -> tuple[lv.Ledger, list[Any]]:
        ledger = lv.Ledger(limits, PRICES)
        inner = Inner()
        first, second = overlapped(ledger, inner, reply())
        a = asyncio.ensure_future(first.responses.create(**request()))
        await inner.started[0].wait()
        refused = await asyncio.gather(second.responses.create(**request()), return_exceptions=True)
        inner.release[0].set()
        await a
        return ledger, refused

    ledger, refused = asyncio.run(scenario())
    assert isinstance(refused[0], lv.LimitExceeded) and ledger.requests == 1
    assert ledger.roles.get("tester") is None  # a refused request is attributed to no one


def test_one_roles_exception_is_never_classified_by_another_roles_requests() -> None:
    ledger = lv.Ledger(_limits(max_requests=10, budget_usd=5.0), PRICES)
    gate = asyncio.Event()

    class RejectsBeforeTransport:
        async def respond(self, *args: Any, **kwargs: Any) -> Any:
            await gate.wait()
            raise ValueError("rejected by validation before any request left")

    async def scenario() -> None:
        inner = Inner()
        inner.script(reply())
        other = lv.GuardedOpenAIClient(inner, ledger, "tester")
        mine = lv.DiagnosticProvider(RejectsBeforeTransport(), ledger, "lead2")
        task = asyncio.ensure_future(mine.respond())
        await asyncio.sleep(0)  # lead2 has taken its snapshot
        done = asyncio.ensure_future(other.responses.create(**request()))
        await inner.started[0].wait()
        inner.release[0].set()
        await done  # the tester made a full request while lead2 was waiting
        gate.set()
        with pytest.raises(ValueError):
            await task

    asyncio.run(scenario())
    assert ledger.requests == 1
    assert [e.stage for e in ledger.errors] == ["rejected_before_transport"]  # not "attempted"


def test_a_roles_exception_after_its_own_response_is_classified_from_its_own_counters() -> None:
    ledger = lv.Ledger(_limits(max_requests=10, budget_usd=5.0), PRICES)

    class AnswersThenFails:
        def __init__(self, client: Any) -> None:
            self.client = client

        async def respond(self, *args: Any, **kwargs: Any) -> Any:
            await self.client.responses.create(**request())
            raise ValueError("adapter could not read the answer")

    async def scenario() -> None:
        inner = Inner()
        inner.script(reply(), reply())
        inner.release[0].set()
        inner.release[1].set()
        busy = lv.GuardedOpenAIClient(inner, ledger, "reviewer")
        await busy.responses.create(**request())  # another role already used the run
        mine = lv.DiagnosticProvider(
            AnswersThenFails(lv.GuardedOpenAIClient(inner, ledger, "lead2")), ledger, "lead2"
        )
        with pytest.raises(ValueError):
            await mine.respond()

    asyncio.run(scenario())
    assert [e.stage for e in ledger.errors] == ["adapter_failure_after_response"]


def test_each_scenarios_role_counters_are_its_own_and_reconcile_with_the_run() -> None:
    client = FakeTeamOpenAI()
    report = run(config(scenarios=("LIVE_FAILING_TESTS", "LIVE_ACCEPTED")), client)
    assert [item.verdict for item in report.scenarios] == ["PASS", "PASS"]
    for index in (0, 1):
        assert sum(role_requests(report, index).values()) == 9  # not the running total
    assert report.attribution_reconciled is True and report.model_requests == 18


def test_a_mismatch_between_roles_and_the_run_is_reported_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = lt.role_usage

    def inflated(*args: Any) -> Any:
        usage = real(*args)
        usage["tester"].requests += 1  # a deliberately wrong attribution
        return usage

    monkeypatch.setattr(lt, "role_usage", inflated)
    report = run(config(), FakeTeamOpenAI())
    assert report.attribution_reconciled is False


# ------------------------------------------------------------------- per-role attribution


def role_requests(report: lt.TeamReport, index: int = 0) -> dict[str, int]:
    return {role: usage.requests for role, usage in report.scenarios[index].roles.items()}


def test_per_role_counters_reconcile_with_the_run_total_when_roles_overlap() -> None:
    client = FakeTeamOpenAI(interleave=True)  # the second Lead and the Tester run at once
    report = run(config(), client)
    item = report.scenarios[0]
    assert report.verdict == "PASS", (item.verdict, item.reason)
    assert role_requests(report) == {"lead": 3, "lead2": 2, "tester": 2, "reviewer": 2}
    assert sum(role_requests(report).values()) == report.model_requests == len(client.requests)
    totals = item.roles.values()
    assert sum(r.input_tokens for r in totals) == report.input_tokens
    assert sum(r.output_tokens for r in totals) == report.output_tokens


def test_a_failing_request_of_one_role_is_attributed_only_to_that_role() -> None:
    class TesterFails(FakeTeamOpenAI):
        async def _create(self, **request: Any) -> Any:
            if any(tool["name"] == "testing__run" for tool in request["tools"]):
                self.requests.append(request)
                await asyncio.sleep(0)
                error = RuntimeError("provider exploded " + SECRET)
                error.status_code = 500  # type: ignore[attr-defined]
                raise error
            return await super()._create(**request)

    report = run(config(), TesterFails(interleave=True), environ={"OPENAI_API_KEY": SECRET})
    roles = report.scenarios[0].roles
    assert roles["tester"].failed_requests == 1
    assert all(usage.failed_requests == 0 for name, usage in roles.items() if name != "tester")
    assert sum(r.requests for r in roles.values()) == report.model_requests
    assert report.verdict != "PASS" and report.provider_error is not None
    assert SECRET not in lt.render(report, {"OPENAI_API_KEY": SECRET})


def test_a_response_without_usage_is_unknown_billing_for_its_own_role_only() -> None:
    class NoUsageForTester(FakeTeamOpenAI):
        async def _create(self, **request: Any) -> Any:
            result = await super()._create(**request)
            if any(tool["name"] == "testing__run" for tool in request["tools"]):
                result.usage = None
            return result

    report = run(config(), NoUsageForTester(interleave=True))
    roles = report.scenarios[0].roles
    assert roles["tester"].unknown_usage_requests == 2
    assert all(u.unknown_usage_requests == 0 for n, u in roles.items() if n != "tester")
    assert report.unknown_usage_requests == 2 and report.cost_is_lower_bound is True
    assert roles["tester"].input_tokens == 0  # unreported usage is not invented


# -------------------------------------------------------------------------- stopping rule


def test_an_inconclusive_first_scenario_never_starts_the_second() -> None:
    # Four responses, all tool calls: the Lead exhausts its steps (the live accepted-path shape).
    client = FakeTeamOpenAI(lead_views=4)
    report = run(config(scenarios=lt.ALL_SCENARIOS[:2]), client)
    assert [item.name for item in report.scenarios] == ["LIVE_ACCEPTED"]
    assert report.scenarios[0].verdict == "INCONCLUSIVE" and report.verdict == "INCONCLUSIVE"
    assert len(client.requests) == 4  # nothing was sent for the second scenario
    assert report.not_started == ["LIVE_FAILING_TESTS"]


def test_a_failed_first_scenario_never_starts_the_second(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lt, "judge", lambda *args: ("FAIL", "forced for this test"))
    client = FakeTeamOpenAI()
    report = run(config(scenarios=lt.ALL_SCENARIOS[:2]), client)
    assert [item.name for item in report.scenarios] == ["LIVE_ACCEPTED"]
    assert report.verdict == "FAIL" and len(client.requests) == 9
    assert report.not_started == ["LIVE_FAILING_TESTS"]


def test_a_passing_first_scenario_lets_the_next_authorized_scenario_run() -> None:
    client = FakeTeamOpenAI()
    names = ("LIVE_FAILING_TESTS", "LIVE_ACCEPTED")  # a negative PASS may continue
    report = run(config(scenarios=names), client)
    assert [item.verdict for item in report.scenarios] == ["PASS", "PASS"]
    assert report.verdict == "PASS" and report.not_started == [] and len(client.requests) == 18


def test_a_provider_failure_stops_the_run_and_keeps_the_first_failures_evidence() -> None:
    class Fails(FakeTeamOpenAI):
        async def _create(self, **request: Any) -> Any:
            self.requests.append(request)
            error = RuntimeError("provider exploded")
            error.status_code = 500  # type: ignore[attr-defined]
            raise error

    client = Fails()
    report = run(config(scenarios=lt.ALL_SCENARIOS[:2]), client)
    assert len(report.scenarios) == 1 and len(client.requests) == 1  # no retry, no next scenario
    assert report.failure is not None and "provider" in report.failure
    assert report.provider_error is not None and report.provider_error["http_status"] == 500


def test_an_authorization_failure_stops_before_any_scenario() -> None:
    client = FakeTeamOpenAI()
    report = run(config(authorization=None, scenarios=lt.ALL_SCENARIOS[:2]), client)
    assert report.scenarios == [] and client.requests == [] and report.verdict == "FAIL"


# ---------------------------------------------------------------------------- diagnostics


def test_a_failed_task_is_reported_with_an_allowlisted_category_and_no_raw_content() -> None:
    client = FakeTeamOpenAI(lead_views=4)
    report = run(config(), client, environ={"OPENAI_API_KEY": SECRET})
    failed = report.scenarios[0].failed_tasks
    assert failed, "a failed task must be explained"
    lead = failed[0]
    assert set(lead) == {"role", "status", "error_code", "category"}
    assert lead["role"] == "lead" and lead["category"] == "max_steps_exceeded"
    assert lead["error_code"] == "execution_error"
    assert report.scenarios[0].roles["lead"].steps == ["tool:lead.view"] * 4  # the shape only


def test_diagnostics_never_copy_arguments_error_text_or_secrets() -> None:
    from tests.test_live_team_validation import response

    class Hostile(FakeTeamOpenAI):
        """A Tester that calls a tool whose name and arguments are the secret itself."""

        async def _create(self, **request: Any) -> Any:
            if "testing__run" in {tool["name"] for tool in request["tools"]}:
                self.requests.append(request)
                return response((SECRET, {"command": SECRET, "note": SECRET}))
            return await super()._create(**request)

    report = run(config(), Hostile(), environ={"OPENAI_API_KEY": SECRET})
    text = lt.render(report, {"OPENAI_API_KEY": SECRET})
    failed = report.scenarios[0].failed_tasks
    assert failed and failed[0]["role"] == "tester" and failed[0]["status"] == "failed"
    assert set(failed[0]) == {"role", "status", "error_code", "category"}
    assert SECRET not in text and SECRET not in json.dumps(failed)
    assert report.scenarios[0].roles["tester"].steps == ["tool:<unregistered>"]
