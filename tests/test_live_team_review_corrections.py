"""M6.4-C targeted review corrections.

Finding 1: a billable run must not start through a supported entry point (`run_team`) without the
authorized-commit check that the command line performs. Finding 2: a negative scenario passes only
when the host's outcome and the durable run status agree that the delivery was *blocked*. Finding 3:
a new live report records the exact verified commit. Deterministic fakes; no model is called.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from scripts import live_team_validation as lt
from scripts import live_validation as lv
from tests.test_live_team_validation import FakeTeamOpenAI, config, run, standin
from tests.test_live_team_verdicts import STATUS, full_tasks, judged

HEAD = "c" * 40


def authorized(head: str | None = HEAD) -> lt.Authorization:
    return lt.Authorization("gpt-5.4-mini", 0.5, ("LIVE_ACCEPTED",), head)


# ------------------------------------------------ Finding 1: the programmatic entry point


@pytest.fixture
def built_clients(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Stands in for the real provider client: records every attempt to build one."""

    built: list[Any] = []

    def build(*args: Any, **kwargs: Any) -> Any:
        client = FakeTeamOpenAI()
        built.append(client)
        return client

    monkeypatch.setattr(lv, "_real_client", build)
    return built


def real_run(cfg: lt.TeamConfig) -> lt.TeamReport:
    # No client injected: this is the path that builds the real provider client and bills.
    return asyncio.run(lt.run_team(cfg, sandbox_factory=standin, environ={"OPENAI_API_KEY": "k"}))


def test_a_programmatic_live_run_without_a_commit_authorization_is_refused(
    built_clients: list[Any],
) -> None:
    report = real_run(config(authorization=authorized(None)))
    assert built_clients == [] and report.model_requests == 0
    assert report.failure is not None and "authorize" in report.failure
    assert report.verdict != "PASS" and report.verified_commit is None


def test_a_programmatic_live_run_whose_commit_check_fails_sends_nothing(
    built_clients: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lt, "verify_authorized_commit", lambda *a, **k: "dirty tree")
    report = real_run(config(authorization=authorized()))
    assert built_clients == [] and report.model_requests == 0
    assert report.failure is not None and "dirty tree" in report.failure
    assert report.verdict != "PASS" and report.verified_commit is None


def test_a_programmatic_live_run_with_a_verified_commit_proceeds_and_records_it(
    built_clients: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[Any] = []

    def verified(head: Any, **options: Any) -> None:
        asked.append(head)

    monkeypatch.setattr(lt, "verify_authorized_commit", verified)
    report = real_run(config(authorization=authorized()))
    assert asked == [HEAD] and len(built_clients) == 1
    assert report.verdict == "PASS" and report.verified_commit == HEAD


def test_a_real_provider_client_injected_by_a_caller_is_held_to_the_same_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An injected `openai` client bills exactly like the one the harness would build."""

    class AsyncOpenAI(FakeTeamOpenAI):
        pass

    AsyncOpenAI.__module__ = "openai._client"
    monkeypatch.setattr(lt, "verify_authorized_commit", lambda *a, **k: "wrong commit")
    client = AsyncOpenAI()
    report = run(config(authorization=authorized()), client)
    assert client.requests == [] and report.model_requests == 0
    assert report.failure is not None and "wrong commit" in report.failure


def test_a_test_fake_client_needs_no_commit_check_and_records_no_verified_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lt, "verify_authorized_commit", lambda *a, **k: pytest.fail("a fake needs no check")
    )
    report = run(config(), FakeTeamOpenAI())
    assert report.verdict == "PASS" and report.verified_commit is None


def test_the_command_line_carries_the_authorized_commit_into_the_authorization() -> None:
    arguments = lt.build_parser().parse_args(
        [
            "run",
            "--live",
            "--authorize-model",
            "gpt-5.4-mini",
            "--authorize-max-usd",
            "0.15",
            "--authorize-scenarios",
            "LIVE_ACCEPTED",
            "--authorize-head",
            f" {HEAD} ",
        ]
    )
    built = lt.config_from(arguments)
    assert built.authorization is not None and built.authorization.head == HEAD


# ------------------------------------------------ Finding 2: blocked means blocked, twice


FAILED_TEST = lt.EvidenceFact("unit.tests", "failed", 1, True)
COMPILED = lt.EvidenceFact("compile.check", "passed", 0, False)


def negative(
    name: str, *, status: Any, run_status: str, reason: str, evidence: lt.EvidenceFact
) -> tuple[str, str]:
    return judged(
        name,
        tasks=[t for t in full_tasks() if t.agent_id in {"lead", "coder", "tester"}],
        status=status,
        run_status=run_status,
        reason=reason,
        evidence=[evidence],
        acceptance="not_evaluated",
    )


def test_the_genuine_gate_block_still_passes() -> None:
    verdict, _ = negative(
        "LIVE_FAILING_TESTS",
        status=STATUS.BLOCKED,
        run_status="blocked",
        reason="quality_blocked",
        evidence=FAILED_TEST,
    )
    assert verdict == "PASS"


@pytest.mark.parametrize("name", ["LIVE_FAILING_TESTS", "LIVE_INVALID_EVIDENCE"])
def test_a_failed_workflow_with_a_failed_run_is_not_a_gate_block(name: str) -> None:
    verdict, why = negative(
        name,
        status=STATUS.FAILED,
        run_status="failed",
        reason="quality_blocked",
        evidence=FAILED_TEST if name == "LIVE_FAILING_TESTS" else COMPILED,
    )
    assert verdict != "PASS" and "blocked" in why


@pytest.mark.parametrize("run_status", ["failed", "running", "completed", "", "BLOCKED"])
def test_a_blocked_outcome_needs_a_blocked_durable_status(run_status: str) -> None:
    verdict, why = negative(
        "LIVE_FAILING_TESTS",
        status=STATUS.BLOCKED,
        run_status=run_status,
        reason="quality_blocked",
        evidence=FAILED_TEST,
    )
    assert verdict != "PASS"
    if run_status != "completed":
        assert "durable run status" in why or "disagree" in why


def test_a_failed_outcome_with_a_blocked_durable_status_is_not_a_pass() -> None:
    verdict, _ = negative(
        "LIVE_FAILING_TESTS",
        status=STATUS.FAILED,
        run_status="blocked",
        reason="quality_blocked",
        evidence=FAILED_TEST,
    )
    assert verdict != "PASS"


# ------------------------------------------------ Finding 3: provenance


def test_a_report_records_the_verified_commit_in_an_allowlisted_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lt, "verify_authorized_commit", lambda *a, **k: None)
    report = run(config(authorization=authorized()), FakeTeamOpenAI())
    data = json.loads(lt.render(report, {}))
    assert (
        data["verified_commit"] is None
    )  # a fake client: nothing was verified, nothing is claimed
    assert "verified_commit" in {f for f in lt.TeamReport.__dataclass_fields__}


def test_a_dry_run_records_no_verified_commit() -> None:
    report = run(config(live=False), FakeTeamOpenAI())
    assert report.verified_commit is None
    assert lv.sanitize_error  # the sanitiser is untouched by this change
