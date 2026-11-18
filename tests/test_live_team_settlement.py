"""M6.4-C R5-3: a reservation is settled exactly once, whatever the provider answers.

A malformed response (no `output`, unreadable or invalid usage, absurd numbers, an exception while
reading it) must never leave a reservation open, never make the request free, and never corrupt the
reported totals. Usage that cannot be trusted is charged at the request's own worst-case estimate
and stops the run, because the budget can no longer be believed. Deterministic fakes only.
"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import live_validation as lv
from tests.test_live_team_accounting import Inner, one_request_cost, request
from tests.test_live_team_validation import PRICES, _limits


def drive(coroutine: Any) -> Any:
    """Run a scenario with a deadline, so a request that is wrongly admitted fails, not hangs."""

    async def bounded() -> Any:
        return await asyncio.wait_for(coroutine, timeout=5)

    return asyncio.run(bounded())


def limits() -> lv.Limits:
    return _limits(max_requests=10, budget_usd=5.0)


USAGE = object()  # the default: normal reported usage


def answer(*, output: Any = (), usage: Any = USAGE) -> Any:
    if usage is USAGE:
        usage = SimpleNamespace(input_tokens=1000, output_tokens=20)
    return SimpleNamespace(id="r", output=output, usage=usage)


def one_call(reply: Any, *, pricing: lv.Pricing = PRICES) -> tuple[lv.Ledger, Any]:
    ledger = lv.Ledger(limits(), pricing)

    async def scenario() -> Any:
        inner = Inner()
        inner.script(reply)
        inner.release[0].set()
        client = lv.GuardedOpenAIClient(inner, ledger, "tester")
        return await client.responses.create(**request())

    return ledger, drive(scenario())


def assert_settled_once(ledger: lv.Ledger, *, estimate_charged: bool) -> None:
    assert ledger.reserved_usd == 0.0 and not ledger._open  # nothing left in flight
    assert ledger.requests == 1 and ledger.responses == 1
    assert ledger.roles["tester"].responses == 1 and ledger.roles["tester"].failed == 0
    if estimate_charged:
        assert ledger.spent_usd == pytest.approx(one_request_cost(limits()))
        assert ledger.usage_responses == 0 and ledger.reported_usd == 0.0


def test_a_normal_response_is_settled_once_with_its_reported_usage() -> None:
    ledger, _ = one_call(answer())
    assert_settled_once(ledger, estimate_charged=False)
    assert ledger.spent_usd == pytest.approx(PRICES.cost(1000, 20)) and ledger.stop_reason is None


@pytest.mark.parametrize("output", [None, 5, object(), "text"], ids=["none", "int", "obj", "str"])
def test_a_malformed_output_still_settles_the_request_with_its_usage(output: Any) -> None:
    ledger, _ = one_call(answer(output=output))
    assert_settled_once(ledger, estimate_charged=False)
    assert ledger.spent_usd == pytest.approx(PRICES.cost(1000, 20))


def test_an_output_that_raises_when_read_still_settles_the_request() -> None:
    class Hostile:
        usage = SimpleNamespace(input_tokens=1000, output_tokens=20)

        @property
        def output(self) -> Any:
            raise RuntimeError("cannot be read")

    ledger, _ = one_call(Hostile())
    assert_settled_once(ledger, estimate_charged=False)


def test_a_response_without_usage_is_unknown_billing_never_free_and_not_a_stop() -> None:
    for usage in (None, SimpleNamespace()):
        ledger, _ = one_call(answer(usage=usage))
        assert_settled_once(ledger, estimate_charged=True)
        assert ledger.stop_reason is None  # unknown is charged at the estimate, the run may go on


@pytest.mark.parametrize(
    "bad",
    ["5", True, -5, 2.5, float("nan"), float("inf"), 10**400, 10**12, [1], {"n": 1}],
    ids=[
        "str",
        "bool",
        "negative",
        "float",
        "nan",
        "inf",
        "huge_int",
        "implausible",
        "list",
        "dict",
    ],
)
def test_usage_that_cannot_be_trusted_is_charged_at_the_estimate_and_stops_the_run(
    bad: Any,
) -> None:
    ledger, _ = one_call(answer(usage=SimpleNamespace(input_tokens=bad, output_tokens=20)))
    assert_settled_once(ledger, estimate_charged=True)
    assert ledger.stop_reason is not None and "usage" in ledger.stop_reason
    assert math.isfinite(ledger.spent_usd) and ledger.input_tokens == 0
    with pytest.raises(lv.LimitExceeded, match="usage"):
        ledger.before(100, "tester")  # nothing more is admitted


def test_untrusted_output_tokens_are_treated_the_same_way() -> None:
    ledger, _ = one_call(answer(usage=SimpleNamespace(input_tokens=1000, output_tokens="many")))
    assert_settled_once(ledger, estimate_charged=True)
    assert ledger.stop_reason is not None


def test_usage_that_raises_when_read_is_untrusted_not_free() -> None:
    class Hostile:
        output: tuple[()] = ()

        @property
        def usage(self) -> Any:
            raise RuntimeError("cannot be read")

    ledger, _ = one_call(Hostile())
    assert_settled_once(ledger, estimate_charged=True)
    assert ledger.stop_reason is not None


def test_an_error_while_pricing_the_usage_charges_the_estimate_and_settles_once() -> None:
    class Booby(lv.Pricing):
        def cost(self, input_tokens: int, output_tokens: int) -> float:
            if input_tokens == 777:
                raise ArithmeticError("cannot price this")
            return super().cost(input_tokens, output_tokens)

    ledger, _ = one_call(
        answer(usage=SimpleNamespace(input_tokens=777, output_tokens=5)),
        pricing=Booby(PRICES.input_per_mtok, PRICES.output_per_mtok),
    )
    assert_settled_once(ledger, estimate_charged=True)
    assert ledger.stop_reason is not None


def test_a_direct_settlement_with_garbage_is_conservative_and_exactly_once() -> None:
    ledger = lv.Ledger(limits(), PRICES)
    reservation = ledger.before(1000, "tester")
    ledger.after("x", None, "y", float("nan"), reservation)  # type: ignore[arg-type]
    spent = ledger.spent_usd
    assert spent == pytest.approx(reservation.estimate) and ledger.reserved_usd == 0.0
    assert math.isfinite(ledger.request_seconds)
    ledger.after(5, 5, 0, 0.0, reservation)  # a second settlement is ignored
    assert ledger.spent_usd == spent and ledger.responses == 1


def test_overlapping_requests_one_malformed_one_valid_are_each_settled_exactly_once() -> None:
    ledger = lv.Ledger(limits(), PRICES)

    async def scenario() -> list[Any]:
        inner = Inner()
        inner.script(
            answer(usage=SimpleNamespace(input_tokens="oops", output_tokens=1)),
            answer(usage=SimpleNamespace(input_tokens=700, output_tokens=10)),
        )
        a = lv.GuardedOpenAIClient(inner, ledger, "lead2")
        b = lv.GuardedOpenAIClient(inner, ledger, "tester")
        first = asyncio.ensure_future(a.responses.create(**request()))
        await inner.started[0].wait()
        second = asyncio.ensure_future(b.responses.create(**request()))
        await inner.started[1].wait()
        inner.release[1].set()
        inner.release[0].set()
        return await asyncio.gather(first, second, return_exceptions=True)

    drive(scenario())
    assert ledger.reserved_usd == 0.0 and not ledger._open and ledger.responses == 2
    assert (
        ledger.roles["lead2"].usage_responses == 0 and ledger.roles["tester"].usage_responses == 1
    )
    assert ledger.spent_usd == pytest.approx(one_request_cost(limits()) + PRICES.cost(700, 10))
    assert ledger.stop_reason is not None  # the malformed usage ended the run


def test_a_cancelled_request_and_a_malformed_response_both_leave_nothing_in_flight() -> None:
    ledger = lv.Ledger(limits(), PRICES)

    async def scenario() -> None:
        inner = Inner()
        inner.script(answer(usage=SimpleNamespace(input_tokens=-1, output_tokens=1)), answer())
        client = lv.GuardedOpenAIClient(inner, ledger, "tester")
        inner.release[0].set()
        await client.responses.create(**request())
        # The run has stopped on the untrusted usage: the next request is not even admitted.
        with pytest.raises(lv.LimitExceeded):
            await client.responses.create(**request())

    drive(scenario())
    assert ledger.reserved_usd == 0.0 and ledger.requests == 1
