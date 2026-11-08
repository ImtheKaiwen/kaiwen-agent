"""M6.3.3-B: the shared `DelegationAdmission` core (ADR 0015, section 4).

The core reuses the central `PolicyEngine`; only an explicit ALLOW admits. These tests pin what it
must do and what it must not claim: approval-like decisions never authorize or modify, a policy
that raises fails closed with a sanitized outcome, host facts that are missing or malformed are
refused before the policy is consulted, and every answer is preliminary and point-in-time (it
reserves nothing). Every wait is bounded.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

import pytest

from kaiwen_agent.policy import (
    AdmissionDecision,
    AdmissionOutcome,
    DelegationAdmission,
    DelegationClaim,
    DelegationPolicy,
    PolicyDecisionType,
    PolicyEngine,
    PolicyRegistry,
)
from tests.test_delegation_job_path import SECRET, Rule

TIMEOUT = 30


def run_async(factory: Any) -> Any:
    async def bounded() -> Any:
        return await asyncio.wait_for(factory(), timeout=TIMEOUT)

    return asyncio.run(bounded())


def admission(*rules: Any) -> DelegationAdmission:
    return DelegationAdmission(policy_engine=PolicyEngine(registry=PolicyRegistry(list(rules))))


def claim(**fields: Any) -> DelegationClaim:
    base: dict[str, Any] = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "agent_id": "lead",
        "delegate_id": "coder",
        "parent_depth": 0,
        "active_delegations": 0,
        "delegations_in_run": 0,
        "caller_capabilities": frozenset({"logs.read", "deploy"}),
        "requested_capabilities": frozenset({"logs.read"}),
    }
    base.update(fields)
    return DelegationClaim(**base)


def limits(**fields: Any) -> DelegationPolicy:
    options: dict[str, Any] = {
        "max_depth": 3,
        "max_parallel_jobs": 2,
        "max_jobs_per_run": 4,
        "allowed_delegates": {"coder"},
    }
    options.update(fields)
    return DelegationPolicy(**options)


# ------------------------------------------------------------------ central policy authority


def test_only_an_explicit_allow_admits_and_the_decision_is_preliminary() -> None:
    async def scenario() -> None:
        decision = await admission(Rule(PolicyDecisionType.ALLOW)).evaluate(claim(parent_depth=2))
        assert decision.admitted and decision.outcome is AdmissionOutcome.ADMITTED
        assert decision.code == "admitted" and decision.child_depth == 3  # parent plus one
        assert decision.policy_id == "test-rule" and decision.rule_id == "test"
        # Not a reservation and not a final authorization, by construction.
        assert decision.preliminary is True and decision.reserves_nothing is True

    run_async(scenario)


def test_deny_and_the_default_decision_fail_closed() -> None:
    async def scenario() -> None:
        denied = await admission(Rule(PolicyDecisionType.DENY, reason="no way")).evaluate(claim())
        assert (denied.outcome, denied.code, denied.reason) == (
            AdmissionOutcome.DENIED,
            "policy_denied",
            "no way",
        )
        assert denied.child_depth is None and not denied.admitted
        default = await admission().evaluate(claim())  # no rule applies: the engine denies
        assert default.outcome is AdmissionOutcome.DENIED and default.code == "policy_denied"

    run_async(scenario)


@pytest.mark.parametrize(
    ("decision", "code"),
    [
        (PolicyDecisionType.REQUIRE_APPROVAL, "approval_required"),
        (PolicyDecisionType.REVIEW, "review_required"),
        (PolicyDecisionType.MODIFY, "modification_required"),
    ],
)
def test_approval_like_decisions_never_admit_and_modifications_are_discarded(
    decision: PolicyDecisionType, code: str
) -> None:
    async def scenario() -> None:
        result = await admission(Rule(decision, reason="needs a human")).evaluate(claim())
        assert result.outcome is AdmissionOutcome.APPROVAL_REQUIRED and not result.admitted
        assert result.code == code and result.reason == "needs a human"
        assert result.child_depth is None
        assert "REWRITTEN" not in result.model_dump_json()  # a MODIFY rewrite is never applied

    run_async(scenario)


def test_the_most_restrictive_decision_wins_across_rules() -> None:
    async def scenario() -> None:
        allow = Rule(PolicyDecisionType.ALLOW)
        review = await admission(
            allow, Rule(PolicyDecisionType.REVIEW, "look", policy_id="review")
        ).evaluate(claim())
        assert review.outcome is AdmissionOutcome.APPROVAL_REQUIRED
        deny = await admission(
            allow,
            Rule(PolicyDecisionType.REVIEW, "look", policy_id="review"),
            Rule(PolicyDecisionType.DENY, "no", policy_id="deny"),
        ).evaluate(claim())
        assert deny.outcome is AdmissionOutcome.DENIED and deny.reason == "no"

    run_async(scenario)


def test_a_policy_that_raises_fails_closed_sanitized_and_unchained(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        caplog.set_level(logging.DEBUG)
        result = await admission(Rule(RuntimeError(SECRET))).evaluate(claim())
        assert result.outcome is AdmissionOutcome.DENIED and result.code == "policy_unavailable"
        assert SECRET not in result.model_dump_json() and "hunter2" not in result.model_dump_json()

    run_async(scenario)
    assert "SECRET" not in caplog.text and "hunter2" not in caplog.text
    assert "RuntimeError" in caplog.text  # the class name is the sanitized signal


def test_cancellation_is_never_swallowed() -> None:
    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await admission(Rule(asyncio.CancelledError())).evaluate(claim())

    run_async(scenario)


def test_policy_reasons_are_bounded_single_line_text() -> None:
    async def scenario() -> None:
        noisy = "line one\x00\x1b[31m\nline two " + "x" * 1_000
        result = await admission(Rule(PolicyDecisionType.DENY, reason=noisy)).evaluate(claim())
        assert len(result.reason) <= 300 and "\n" not in result.reason
        assert "\x00" not in result.reason and "\x1b" not in result.reason
        assert result.reason.startswith("line one")

    run_async(scenario)


# ------------------------------------------------------------------ the existing limits, unchanged


def test_depth_parallel_and_per_run_limits_are_enforced_at_their_boundaries() -> None:
    async def scenario() -> None:
        engine = admission(limits())
        assert (await engine.evaluate(claim(parent_depth=2))).admitted
        too_deep = await engine.evaluate(claim(parent_depth=3))
        assert too_deep.code == "policy_denied" and "depth" in too_deep.reason
        assert (await engine.evaluate(claim(active_delegations=1))).admitted
        busy = await engine.evaluate(claim(active_delegations=2))
        assert busy.code == "policy_denied" and "parallel" in busy.reason
        assert (await engine.evaluate(claim(delegations_in_run=3))).admitted
        spent = await engine.evaluate(claim(delegations_in_run=4))
        assert spent.code == "policy_denied" and "per run" in spent.reason
        unknown = await engine.evaluate(claim(delegate_id="ghost"))
        assert unknown.code == "policy_denied" and "allowlisted" in unknown.reason

    run_async(scenario)


def test_the_policy_context_is_built_from_host_facts_for_each_kind() -> None:
    async def scenario() -> None:
        rule = Rule(PolicyDecisionType.ALLOW)
        engine = admission(rule)
        await engine.evaluate(claim(parent_id="job-1"))
        await engine.evaluate(
            claim(parent_id="task-1", kind="workflow", run_id="run-1", caller_role="lead")
        )
        await engine.evaluate(claim(parent_id="task-2", kind="workflow", run_id="run-1"))
        job, workflow, unnamed = rule.contexts
        assert job.metadata == {
            "delegation_depth": 0,
            "parallel_jobs": 0,
            "jobs_in_run": 0,
            "requested_capabilities": ["logs.read"],
            "parent_job_id": "job-1",
        }
        assert workflow.metadata["parent_task_id"] == "task-1"
        assert workflow.metadata["delegation_kind"] == "workflow"
        assert workflow.metadata["delegator_role"] == "lead"  # a host-assigned role, for policies
        assert "delegator_role" not in unnamed.metadata and "delegator_role" not in job.metadata
        assert "parent_job_id" not in workflow.metadata
        assert (workflow.action, workflow.resource, workflow.run_id) == (
            "agent.delegate",
            "coder",
            "run-1",
        )
        assert workflow.capabilities == frozenset({"logs.read", "deploy"})

    run_async(scenario)


# ------------------------------------------------------------------ bounds checked before policy


def test_capability_escalation_is_denied_before_the_policy_is_consulted() -> None:
    async def scenario() -> None:
        rule = Rule(PolicyDecisionType.ALLOW)
        engine = admission(rule)
        escalate = await engine.evaluate(claim(requested_capabilities=frozenset({"admin"})))
        assert escalate.code == "capability_escalation" and not escalate.admitted
        parent = await engine.evaluate(
            claim(
                requested_capabilities=frozenset({"deploy"}),
                parent_capabilities=frozenset({"logs.read"}),
            )
        )
        assert parent.code == "capability_exceeds_parent"
        within = await engine.evaluate(
            claim(
                requested_capabilities=frozenset({"logs.read"}),
                parent_capabilities=frozenset({"logs.read"}),
            )
        )
        assert within.admitted
        assert len(rule.contexts) == 1  # only the admissible request reached the policy

    run_async(scenario)


def test_resource_keys_are_checked_only_when_an_allowlist_is_given() -> None:
    async def scenario() -> None:
        rule = Rule(PolicyDecisionType.ALLOW)
        engine = admission(rule)
        keys = ("repo:main", "anything")
        assert (await engine.evaluate(claim(resource_keys=keys))).admitted  # legacy: unrestricted
        listed = frozenset({"repo:main"})
        denied = await engine.evaluate(claim(resource_keys=keys, resource_allowlist=listed))
        assert denied.code == "resource_not_allowed" and "resource" in denied.reason
        assert (
            await engine.evaluate(claim(resource_keys=("repo:main",), resource_allowlist=listed))
        ).admitted
        none_needed = await engine.evaluate(claim(resource_allowlist=frozenset()))
        assert none_needed.admitted  # no keys requested, nothing to deny
        empty = await engine.evaluate(claim(resource_keys=("x",), resource_allowlist=frozenset()))
        assert empty.code == "resource_not_allowed"  # an empty allowlist denies every key

    run_async(scenario)


@pytest.mark.parametrize(
    "bad",
    [
        {"tenant_id": ""},
        {"user_id": ""},
        {"session_id": ""},
        {"agent_id": ""},
        {"delegate_id": ""},
        {"parent_depth": -1},
        {"active_delegations": -1},
        {"delegations_in_run": -1},
        {"parent_depth": True},
        {"active_delegations": 1.0},
        {"delegations_in_run": "0"},
        {"caller_capabilities": {"logs.read"}},
        {"requested_capabilities": ["logs.read"]},
        {"parent_capabilities": {"logs.read"}},
        {"resource_keys": ["repo:main"]},
        {"resource_keys": ("",)},
        {"resource_allowlist": {"repo:main"}},
        {"caller_role": ""},
        {"caller_role": 5},
    ],
    ids=lambda bad: next(iter(bad)) + "=" + repr(next(iter(bad.values()))),
)
def test_missing_or_malformed_host_facts_fail_closed_before_the_policy(bad: dict[str, Any]) -> None:
    async def scenario() -> None:
        rule = Rule(PolicyDecisionType.ALLOW)
        result = await admission(rule).evaluate(replace(claim(), **bad))
        assert result.outcome is AdmissionOutcome.DENIED
        assert result.code == "invalid_trusted_state" and not result.admitted
        assert rule.contexts == []  # the policy never saw untrustworthy facts

    run_async(scenario)


# ------------------------------------------------------------------ structure and concurrency


def test_the_core_holds_no_submission_or_state_handles() -> None:
    engine = admission(Rule(PolicyDecisionType.ALLOW))
    assert set(vars(engine)) == {"policy_engine"}  # no job manager, task store or scheduler


def test_decisions_are_immutable_and_cannot_claim_a_reservation() -> None:
    async def scenario() -> None:
        decision = await admission(Rule(PolicyDecisionType.ALLOW)).evaluate(claim())
        with pytest.raises(Exception):  # noqa: B017 - frozen model
            decision.outcome = AdmissionOutcome.DENIED  # type: ignore[misc]
        with pytest.raises(ValueError):
            AdmissionDecision(
                outcome=AdmissionOutcome.ADMITTED,
                code="admitted",
                reason="x",
                reserves_nothing=False,  # type: ignore[arg-type]
            )

    run_async(scenario)


def test_concurrent_evaluations_are_independent_and_point_in_time() -> None:
    """Preliminary answers reserve nothing: with one slot left, every concurrent caller is admitted.

    This pins the documented limit (ADR 0015, 4.2). It is not a claim of atomic admission; an
    authoritative final admission must re-check where work becomes executable.
    """

    async def scenario() -> None:
        engine = admission(limits(max_parallel_jobs=1))
        results = await asyncio.gather(*(engine.evaluate(claim()) for _ in range(8)))
        assert all(item.admitted and item.reserves_nothing for item in results)
        # A fresh evaluation sees fresh facts, not a cached or reserved answer.
        busy = await engine.evaluate(claim(active_delegations=1))
        assert busy.outcome is AdmissionOutcome.DENIED and "parallel" in busy.reason
        mixed = await asyncio.gather(
            engine.evaluate(claim(active_delegations=1)),
            engine.evaluate(claim(active_delegations=0)),
            engine.evaluate(claim(parent_depth=3)),
        )
        assert [item.admitted for item in mixed] == [False, True, False]

    run_async(scenario)
