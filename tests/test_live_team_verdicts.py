"""M6.4-C R5-2 and R5-4: what a PASS requires, judged from host records only.

A live `LIVE_ACCEPTED` PASS needs all four roles to have taken part, shown by the durable task
records, the host's record of each registered tool call and the model requests the ledger counted:
never by what a model said. A negative scenario passes only when the container reported a real test
failure, not when an infrastructure problem left a failing exit code behind. Deterministic fakes;
nothing here calls a model, and a fake PASS says nothing about a real provider.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kaiwen_agent.toolkits import SandboxResult
from scripts import live_team_validation as lt
from scripts import live_validation as lv
from tests.test_live_team_validation import (
    PRICES,
    FakeTeamOpenAI,
    StandIn,
    _limits,
    config,
    example,
    response,
    run,
)

ROLES = ("lead", "lead2", "tester", "reviewer")
STATUS = lt.WorkflowOutcomeStatus


# ---------------------------------------------------------------- a Lead that skips the second Lead


class SameCycleLead(FakeTeamOpenAI):
    """The Lead proposes the review itself, so the second Lead is never needed or asked."""

    async def _create(self, **request: Any) -> Any:
        names = {tool["name"] for tool in request["tools"]}
        instructions = request["instructions"]
        is_lead = "lead__propose_task" in names and "Do not call lead.view" not in instructions
        if is_lead and self.outputs(request) == self.lead_views:
            self.requests.append(request)
            from tests.test_live_team_validation import propose

            return response(
                propose(id="code", name="Fix add()", role="coder"),
                propose(id="test", name="Run the tests", role="tester", dependencies=["code"]),
                propose(
                    id="review",
                    name="Review",
                    role="reviewer",
                    dependencies=["code", "test"],
                ),
                tokens=self.tokens,
            )
        return await super()._create(**request)


def test_a_delivery_completed_without_the_second_lead_is_not_a_pass() -> None:
    client = SameCycleLead()
    report = run(config(), client)
    item = report.scenarios[0]
    assert item.outcome_status == "completed" and item.team_acceptance == "accepted:accepted"
    assert item.roles["lead2"].requests == 0  # it never took part
    assert item.verdict == "FAIL" and "lead2" in (item.reason or "")
    assert report.verdict == "FAIL"


def test_the_ordinary_accepted_delivery_still_passes_with_every_role_on_record() -> None:
    report = run(config(), FakeTeamOpenAI())
    item = report.scenarios[0]
    assert item.verdict == "PASS" and report.verdict == "PASS"
    assert all(item.roles[role].requests > 0 for role in ROLES)
    assert item.participation == {role: [] for role in ROLES}  # no problem recorded for any role


# ------------------------------------------------------------------------------ the judge, directly


def task(
    role: str, *, status: str = "completed", calls: tuple[str, ...] = (), **output: Any
) -> Any:
    record = {"tool_calls": [{"tool": name, "is_error": False} for name in calls], **output}
    return SimpleNamespace(agent_id=role, status=SimpleNamespace(value=status), output=record)


def full_tasks() -> list[Any]:
    return [
        task("lead", calls=("lead.view", "lead.propose_task"), delegation_proposals=[{}]),
        task("coder"),
        task("lead2", calls=("lead.propose_task",), delegation_proposals=[{}]),
        task("tester", calls=("testing.run",)),
        task("reviewer", calls=("review.submit",), review_assessment={"x": 1}),
    ]


def used(**overrides: int) -> dict[str, lt.RoleUsage]:
    return {role: lt.RoleUsage(requests=overrides.get(role, 2)) for role in ROLES}


PASSED = lt.EvidenceFact("unit.tests", "passed", 0, False)


def judged(
    name: str = "LIVE_ACCEPTED",
    *,
    tasks: list[Any] | None = None,
    roles: dict[str, lt.RoleUsage] | None = None,
    evidence: list[lt.EvidenceFact] | None = None,
    status: Any = STATUS.COMPLETED,
    run_status: str = "completed",
    reason: str | None = None,
    acceptance: str = "accepted:accepted",
) -> tuple[str, str]:
    result = SimpleNamespace(
        outcome=SimpleNamespace(
            status=status, reason=reason, tasks=full_tasks() if tasks is None else tasks
        ),
        run_status=run_status,
        acceptance=acceptance,
    )
    ledger = lv.Ledger(_limits(), PRICES)
    facts = [PASSED] if evidence is None else evidence
    return lt.judge(
        lt.LIVE_SCENARIOS[name], result, facts, ledger, used() if roles is None else roles
    )


def test_the_judge_passes_a_fully_recorded_accepted_delivery() -> None:
    assert judged()[0] == "PASS"


@pytest.mark.parametrize("missing", ROLES)
def test_a_missing_role_task_prevents_a_pass(missing: str) -> None:
    tasks = [t for t in full_tasks() if t.agent_id != missing]
    verdict, reason = judged(tasks=tasks)
    assert verdict == "FAIL" and missing in reason


@pytest.mark.parametrize("missing", ROLES)
def test_a_role_that_made_no_model_request_prevents_a_pass(missing: str) -> None:
    verdict, reason = judged(roles=used(**{missing: 0}))
    assert verdict == "FAIL" and missing in reason


@pytest.mark.parametrize(
    ("role", "tool"),
    [("lead", "lead.propose_task"), ("lead2", "lead.propose_task"), ("tester", "testing.run")]
    + [("reviewer", "review.submit")],
)
def test_a_role_without_its_registered_tool_call_on_record_prevents_a_pass(
    role: str, tool: str
) -> None:
    tasks = full_tasks()
    for item in tasks:
        if item.agent_id == role:
            item.output["tool_calls"] = [{"tool": tool, "is_error": True}]  # failed call
    verdict, reason = judged(tasks=tasks)
    assert verdict == "FAIL" and role in reason


def test_a_reviewer_without_a_recorded_review_assessment_prevents_a_pass() -> None:
    tasks = full_tasks()
    tasks[-1].output.pop("review_assessment")
    assert judged(tasks=tasks)[0] == "FAIL"


def test_a_failed_role_task_prevents_a_pass() -> None:
    tasks = full_tasks()
    tasks[3] = task("tester", status="failed", calls=("testing.run",))
    assert judged(tasks=tasks)[0] == "FAIL"


def test_unauthenticated_or_non_passing_evidence_prevents_a_pass() -> None:
    assert judged(evidence=[])[0] == "FAIL"
    assert judged(evidence=[lt.EvidenceFact("unit.tests", "failed", 1, True)])[0] == "FAIL"
    assert judged(evidence=[lt.EvidenceFact("compile.check", "passed", 0, False)])[0] == "FAIL"


def test_an_attribution_mismatch_prevents_the_overall_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    real = lt.role_usage

    def inflated(*args: Any) -> Any:
        usage = real(*args)
        usage["tester"].requests += 1
        return usage

    monkeypatch.setattr(lt, "role_usage", inflated)
    report = run(config(), FakeTeamOpenAI())
    assert report.attribution_reconciled is False
    assert report.verdict == "FAIL" and report.scenarios[0].verdict == "FAIL"
    assert "reconcile" in (report.scenarios[0].reason or "")


def test_a_negative_scenario_does_not_need_the_later_roles() -> None:
    only_tester = [task("lead"), task("tester", calls=("testing.run",))]
    failed = lt.EvidenceFact("unit.tests", "failed", 1, True)
    verdict, _ = judged(
        "LIVE_FAILING_TESTS",
        tasks=only_tester,
        roles=used(lead2=0, reviewer=0),
        evidence=[failed],
        status=STATUS.BLOCKED,
        run_status="blocked",
        reason="quality_blocked",
        acceptance="not_evaluated",
    )
    assert verdict == "PASS"


# ----------------------------------------------------------- a negative scenario and the container


class BrokenInfrastructure(StandIn):
    """TEST ONLY. The command 'ran' but the container machinery failed, not the tests."""

    mode = "docker_error"

    async def run(self, command: Any) -> SandboxResult:
        result = await super().run(command)
        if self.mode == "docker_error":
            return result.model_copy(update={"exit_code": 125, "stdout": "docker: daemon error"})
        if self.mode == "timeout":
            return result.model_copy(update={"exit_code": 124, "timed_out": True, "stdout": ""})
        if self.mode == "import_error":
            return result.model_copy(
                update={"exit_code": 1, "stdout": "ImportError\nFAILED (errors=1)\n"}
            )
        return result


def failing(mode: str) -> Any:
    def factory(filesystem: Any) -> Any:
        runner = BrokenInfrastructure(example.SnapshotBuilder(filesystem, "main"))
        runner.mode = mode
        return runner

    return factory


@pytest.mark.parametrize("mode", ["docker_error", "timeout", "import_error"])
def test_an_infrastructure_failure_is_not_a_failing_test_pass(mode: str) -> None:
    cfg = config(scenarios=("LIVE_FAILING_TESTS",))
    report = asyncio.run(
        lt.run_team(cfg, client=FakeTeamOpenAI(), sandbox_factory=failing(mode), environ={})
    )
    item = report.scenarios[0]
    assert item.run_status != "completed"  # the host still never completed it
    assert item.verdict == "INCONCLUSIVE", (mode, item.verdict, item.reason)
    assert "assertion failure" in (item.reason or "")


def test_a_real_assertion_failure_in_the_container_is_still_a_pass() -> None:
    report = run(config(scenarios=("LIVE_FAILING_TESTS",)), FakeTeamOpenAI())
    item = report.scenarios[0]
    assert item.verdict == "PASS", (item.verdict, item.reason)
    assert item.evidence == [
        {"command_id": "unit.tests", "outcome": "failed", "exit_code": 1, "assertion_failure": True}
    ]


# ----------------------------------------------------------- what LIVE_INVALID_EVIDENCE verifies


def test_the_invalid_evidence_scenario_is_a_completion_gate_rejection_and_says_so() -> None:
    report = run(config(scenarios=("LIVE_INVALID_EVIDENCE",)), FakeTeamOpenAI())
    item = report.scenarios[0]
    assert item.verdict == "PASS", (item.verdict, item.reason)
    assert item.rejected_by == "completion_gate" and item.outcome_reason == "quality_blocked"
    assert [e["command_id"] for e in item.evidence] == ["compile.check"]
    description = lt.LIVE_SCENARIOS["LIVE_INVALID_EVIDENCE"].description
    assert "completion gate" in description and "team acceptance" not in description


def test_each_participation_problem_is_named_in_the_report_record() -> None:
    result = SimpleNamespace(
        outcome=SimpleNamespace(tasks=[task("tester", status="failed", calls=("testing.run",))])
    )
    problems = lt.participation_problems(result, {"tester": lt.RoleUsage(requests=2)})
    assert "no completed task record" in problems["tester"]
    assert "no model request was counted" in problems["lead"]
    assert "no completed task record" in problems["lead2"]


def test_a_negative_scenario_needs_the_testers_registered_call_on_record() -> None:
    failed = lt.EvidenceFact("unit.tests", "failed", 1, True)
    blocked = {
        "status": STATUS.BLOCKED,
        "run_status": "blocked",
        "reason": "quality_blocked",
        "acceptance": "not_evaluated",
        "evidence": [failed],
    }
    assert judged("LIVE_FAILING_TESTS", **blocked)[0] == "PASS"
    no_call = [task(role) for role in ROLES]  # completed tasks, but no recorded tool call
    verdict, reason = judged("LIVE_FAILING_TESTS", tasks=no_call, **blocked)
    assert verdict == "INCONCLUSIVE" and "not on record" in reason


def test_the_invalid_evidence_scenario_needs_its_compile_check_to_have_passed() -> None:
    blocked = {
        "status": STATUS.BLOCKED,
        "run_status": "blocked",
        "reason": "quality_blocked",
        "acceptance": "not_evaluated",
    }
    good = [lt.EvidenceFact("compile.check", "passed", 0, False)]
    bad = [lt.EvidenceFact("compile.check", "timed_out", 124, False)]
    assert judged("LIVE_INVALID_EVIDENCE", evidence=good, **blocked)[0] == "PASS"
    assert judged("LIVE_INVALID_EVIDENCE", evidence=bad, **blocked)[0] == "INCONCLUSIVE"


@pytest.mark.parametrize("reconciled", [False, None])
def test_the_overall_verdict_is_never_pass_without_reconciled_accounting(
    reconciled: bool | None,
) -> None:
    report = lt.TeamReport(mode="live", provider="openai", model="m", limits={})
    report.scenarios.append(lt.TeamScenarioReport(name="LIVE_ACCEPTED", expectation="x"))
    report.scenarios[0].verdict = "PASS"
    report.attribution_reconciled = reconciled
    report.finish()
    assert report.verdict == "FAIL"
    report.attribution_reconciled = True
    report.finish()
    assert report.verdict == "PASS"
