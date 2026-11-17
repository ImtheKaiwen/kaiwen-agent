"""M6.4-C: controlled live validation of the multi-role coding team. Manual; never run by CI.

The team is the one of `examples/coding-team-acceptance`: Lead, a second Lead, a model-driven
Tester and a Reviewer are real model calls (through the existing guarded OpenAI client and request
ledger), the Coder stays a host function, and `testing.run` executes only in `DockerSandboxRunner`.
Nothing here orchestrates, authorizes or verifies anything itself: the example's composition (fenced
SQLite, delegation, trusted evidence pipeline, completion gate, team acceptance) decides, and this
script only supplies the models, enforces the limits across all roles combined and reports.

A model never chooses a command line, a path, an image or a permission. It names registered tools
and registered command ids from fixed, host-written briefs; the host validates every call. What a
model says is never evidence. Briefs direct the models (the Reviewer is told what to recommend), so
a PASS shows that real models can drive the registered tool schemas and that the host's gates
hold, not that the models judge code well.

Subcommands:

    preflight   no-cost key, authentication and model-availability check (no generation)
    plan        offline plan and authorization request (no key, no model, no Docker)
    run         dry run by default; `--live` needs an explicit authorization (model, maximum
                spend, scenarios) and makes billed requests
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # `python scripts/<this file>`

from kaiwen_agent.artifacts import ArtifactContext  # noqa: E402
from kaiwen_agent.toolkits import DockerSandboxConfig, SandboxUnavailableError  # noqa: E402
from kaiwen_agent.workflow import WorkflowOutcomeStatus  # noqa: E402
from scripts import live_validation as lv  # noqa: E402

EXAMPLE = lv.ROOT / "examples" / "coding-team-acceptance" / "main.py"
PROMPT_OF_ROLE = {
    "lead": "coding.lead",
    "lead2": "coding.lead",
    "tester": "coding.tester",
    "reviewer": "coding.reviewer",
}
MODEL_REQUESTS_PER_DELIVERY = lv.TEAM_REQUESTS_PER_DELIVERY
DEFAULT_MAX_REQUESTS = 20  # the two recommended scenarios need 18; two requests of slack
DEFAULT_MAX_TOOL_CALLS = 10
DEFAULT_MAX_INPUT_TOKENS = 6_000  # the largest planned request is about 4,000 by the local estimate
DEFAULT_BUDGET_USD = 0.25


# ------------------------------------------------------------------------------------ scenarios


@dataclass(frozen=True, slots=True)
class LiveScenario:
    """What differs between live runs. The platform's requirements never change."""

    name: str
    description: str
    fix_code: bool
    command: str  # the one registered command the Tester brief names
    cite: str  # which command's evidence the example's own scripted checks would cite
    negative: bool
    accepted_reasons: frozenset[str] = frozenset()


LIVE_SCENARIOS: dict[str, LiveScenario] = {
    "LIVE_ACCEPTED": LiveScenario(
        "LIVE_ACCEPTED",
        "the delivery is fixed, tested in the container, reviewed and accepted",
        fix_code=True,
        command="unit.tests",
        cite="tests",
        negative=False,
    ),
    "LIVE_FAILING_TESTS": LiveScenario(
        "LIVE_FAILING_TESTS",
        "the bug is left in place: the container run fails and the delivery is never completed",
        fix_code=False,
        command="unit.tests",
        cite="tests",
        negative=True,
        accepted_reasons=frozenset({"quality_blocked"}),
    ),
    "LIVE_INVALID_EVIDENCE": LiveScenario(
        "LIVE_INVALID_EVIDENCE",
        "only unrelated evidence exists (the compile check): the delivery is never completed",
        fix_code=True,
        command="compile.check",
        cite="compile",
        negative=True,
        accepted_reasons=frozenset(
            {"quality_blocked", "team_changes_required:evidence_not_gate_verified"}
        ),
    ),
}
ALL_SCENARIOS = tuple(LIVE_SCENARIOS)
# Nothing is billed for these two unless the operator names them: the smallest useful live set.
RECOMMENDED_SCENARIOS = ("LIVE_ACCEPTED", "LIVE_FAILING_TESTS")


def briefs(scenario: LiveScenario) -> dict[str, str]:
    """Fixed, host-written instructions appended to each registered role prompt."""

    return {
        "lead": (
            "Call lead.view once. Then, in ONE response, call lead.propose_task three times with "
            "exactly these tasks and nothing else: (1) id 'code', name 'Fix add()', role 'coder', "
            "description 'Fix add() in pkg/calc.py so it returns a + b.'; (2) id 'test', name 'Run "
            "the tests', role 'tester', dependencies ['code'], description 'Run the registered "
            "test command.'; (3) id 'plan2', name 'Plan the review', role 'lead2', description "
            "'Plan the review of the delivery.'. After the tool results, answer in one short "
            "sentence."
        ),
        "lead2": (
            "Do not call lead.view. Call lead.propose_task exactly once with: id 'review', name "
            "'Review the delivery', role 'reviewer', description 'Review the delivery against its "
            "acceptance criteria.'. Propose nothing else. After the tool result, answer in one "
            "short sentence."
        ),
        "tester": (
            f"Call testing.run exactly once with command_id '{scenario.command}' and parameters "
            "set to an empty object. Call no other tool and no other command. Afterwards report in "
            "one sentence what the tool returned. Your report is a claim, not evidence."
        ),
        "reviewer": (
            "Call review.submit exactly once with recommendation 'approve', reviewed_tasks "
            "['code', 'test'], one criterion {criterion: 'add() returns the sum', verdict: 'met'} "
            "and one finding {severity: 'info', text: 'Relied on the test run evidence', "
            "evidence_refs: the host-authenticated evidence reference(s) listed in your context}. "
            "Copy the reference(s) exactly and never invent one. Then answer in one short "
            "sentence."
        ),
    }


# ------------------------------------------------------------------------------------- reports


@dataclass(slots=True)
class RoleUsage:
    """One role's share of the shared ledger, for one scenario.

    Every request is attributed to exactly one role when it is admitted and settled, so the roles
    always add up to the scenario total (`TeamReport.attribution_reconciled`). Tokens are the
    provider-reported usage only; a request without usage is counted in `unknown_usage_requests`
    (a failed or cancelled request is one of them) and never as zero cost.
    """

    requests: int = 0
    responses: int = 0
    responses_with_usage: int = 0
    failed_requests: int = 0
    unknown_usage_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # The shape of the conversation only, e.g. `tool:lead.view`, `tool:lead.propose_task x3`,
    # `text`; tool names come from the registered tool set, never from the model's own words.
    steps: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TeamScenarioReport:
    name: str
    expectation: str
    verdict: str = "NOT_RUN"  # PASS, FAIL or INCONCLUSIVE; never inferred from model text
    outcome_status: str | None = None
    outcome_reason: str | None = None
    run_status: str | None = None
    team_acceptance: str | None = None
    evidence_commands: list[str] = field(default_factory=list)
    roles: dict[str, RoleUsage] = field(default_factory=dict)
    # Allowlisted facts about each task that did not complete: role, status, error code, category.
    failed_tasks: list[dict[str, str]] = field(default_factory=list)
    reason: str | None = None
    elapsed_seconds: float = 0.0


@dataclass(slots=True)
class TeamReport:
    mode: str
    provider: str
    model: str
    limits: dict[str, Any]
    scenarios: list[TeamScenarioReport] = field(default_factory=list)
    docker: str = "not_checked"
    model_requests: int = 0
    successful_responses: int = 0
    responses_with_usage: int = 0
    unknown_usage_requests: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    cost_is_lower_bound: bool = False
    billing_note: str | None = None
    worst_case_cost_usd: float | None = None
    request_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    abort_reason: str | None = None
    provider_error: dict[str, Any] | None = None
    failure: str | None = None
    authorization: dict[str, Any] | None = None
    # Scenarios that never began because an earlier one did not PASS or the run stopped.
    not_started: list[str] = field(default_factory=list)
    # True when the per-role counters add up to the run totals (requests and reported tokens).
    attribution_reconciled: bool | None = None
    verdict: str = "NOT_RUN"

    def finish(self) -> None:
        if self.mode != "live":
            self.verdict = "NOT_RUN"
            return
        verdicts = [item.verdict for item in self.scenarios]
        if self.failure is not None and not self.scenarios:
            self.verdict = "FAIL" if self.abort_reason is None else "INCONCLUSIVE"
        elif "FAIL" in verdicts:
            self.verdict = "FAIL"
        elif not verdicts or "INCONCLUSIVE" in verdicts or self.failure is not None:
            self.verdict = "INCONCLUSIVE"
        else:
            self.verdict = "PASS" if all(v == "PASS" for v in verdicts) else "INCONCLUSIVE"


def render(report: TeamReport | dict[str, Any], environ: Mapping[str, str] | None = None) -> str:
    data = asdict(report) if isinstance(report, TeamReport) else report
    text = json.dumps(data, indent=2, sort_keys=True, allow_nan=False)
    return lv._Redaction(os.environ if environ is None else environ).apply(text)


# ------------------------------------------------------------------------- configuration, plan


@dataclass(frozen=True, slots=True)
class Authorization:
    """The operator's separate, explicit permission for one paid run."""

    model: str
    max_usd: float
    scenarios: tuple[str, ...]

    def covers(self, config: TeamConfig) -> str | None:
        """None when the run is exactly what was authorized, otherwise why it is refused."""

        if not (math.isfinite(self.max_usd) and self.max_usd > 0):
            return "the authorized maximum spend must be a finite number greater than zero"
        if self.model != config.model:
            return "the authorized model differs from the configured model"
        if set(self.scenarios) != set(config.scenarios):
            return "the authorized scenarios differ from the configured scenarios"
        if config.limits.budget_usd > self.max_usd:
            return "the configured budget exceeds the authorized maximum spend"
        return None


@dataclass(frozen=True, slots=True)
class TeamConfig:
    model: str = lv.DEFAULT_OPENAI_MODEL
    scenarios: tuple[str, ...] = RECOMMENDED_SCENARIOS
    live: bool = False
    limits: lv.Limits = field(
        default_factory=lambda: lv.Limits(
            max_requests=DEFAULT_MAX_REQUESTS,
            max_steps=4,
            max_tool_calls=DEFAULT_MAX_TOOL_CALLS,
            max_output_tokens=1_500,
            max_input_tokens=DEFAULT_MAX_INPUT_TOKENS,
            request_timeout=90.0,
            total_timeout=900.0,
            budget_usd=DEFAULT_BUDGET_USD,
        )
    )
    pricing: lv.Pricing = field(default_factory=lv.Pricing)
    image: str = lv.DEFAULT_IMAGE
    docker_path: str | None = None  # a trusted override of the Docker client, for tests
    authorization: Authorization | None = None

    def validate(self) -> None:
        self.limits.validate()
        self.pricing.validate()
        if not self.scenarios or any(name not in LIVE_SCENARIOS for name in self.scenarios):
            raise ValueError(
                "unknown or empty scenario list; available: " + ", ".join(ALL_SCENARIOS)
            )
        if len(set(self.scenarios)) != len(self.scenarios):
            raise ValueError("a scenario may be listed once")
        if self.limits.max_steps > 4:
            raise ValueError(
                "the example bounds every task at 4 model steps; a higher limit is void"
            )


def plan(config: TeamConfig) -> dict[str, Any]:
    """The offline readiness assessment and authorization request for exactly these scenarios."""

    config.validate()
    result = lv.team_plan(config.limits, config.pricing, config.model, config.scenarios)
    result["roles"] = list(PROMPT_OF_ROLE)
    result["max_tool_calls_per_scenario"] = config.limits.max_tool_calls
    return result


# --------------------------------------------------------------------------- the example, models


def load_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("coding_team_acceptance_live", EXAMPLE)
    if spec is None or spec.loader is None:
        raise lv.HarnessError("the coding team example is not available")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RoleModel:
    """One role's real model: the registered role prompt plus its host-written brief.

    The role's provider talks through a guarded client that carries the role, so the shared ledger
    both enforces the run-wide limits and attributes each request to exactly this role. This class
    only records the shape of each answer (registered tool names, or text).
    """

    def __init__(self, role: str, provider: Any, steps: list[str]) -> None:
        self.role = role
        self._provider = provider
        self._steps = steps

    async def respond(self, *args: Any, **kwargs: Any) -> Any:
        response = await self._provider.respond(*args, **kwargs)
        registered = {str(getattr(tool, "name", "")) for tool in kwargs.get("tools") or ()}
        self._steps.append(_shape(response, registered))
        return response


def _shape(response: Any, registered: set[str]) -> str:
    calls = [str(getattr(call, "name", "")) for call in getattr(response, "tool_calls", None) or []]
    if not calls:
        return "text" if getattr(response, "text", None) else "empty"
    first = calls[0] if calls[0] in registered else "<unregistered>"
    count = f" x{len(calls)}" if len(calls) > 1 else ""
    if len(set(calls)) > 1:
        return f"tools:{len(calls)} mixed"
    return f"tool:{first}{count}"


def model_factory_for(
    scenario: LiveScenario,
    config: TeamConfig,
    inner: Any,
    ledger: lv.Ledger,
    steps: dict[str, list[str]],
) -> Callable[[str, list[Any]], Any]:
    """`inner` is the unguarded client: every role wraps it in its own role-tagged guard."""

    texts = briefs(scenario)
    provider_config = lv.Config(provider="openai", model=config.model, live=True)

    def factory(role: str, scripted_steps: list[Any]) -> Any:
        del scripted_steps  # the deterministic example's script; a live role ignores it
        if role not in PROMPT_OF_ROLE:
            raise lv.HarnessError(f"no live model is defined for the role {role!r}")
        base, _, _ = lv.load_instructions(PROMPT_OF_ROLE[role])
        instructions = f"{base}\n\nVALIDATION BRIEF (host-authored, fixed)\n{texts[role]}"
        guarded = lv.GuardedOpenAIClient(inner, ledger, role)
        provider = lv.DiagnosticProvider(
            lv.build_provider(provider_config, guarded, instructions), ledger, role
        )
        return RoleModel(role, provider, steps.setdefault(role, []))

    return factory


def role_usage(
    before: dict[str, lv.RoleLedger], after: dict[str, lv.RoleLedger], steps: dict[str, list[str]]
) -> dict[str, RoleUsage]:
    """This scenario's share of each role: the ledger now minus the ledger when it began."""

    usage: dict[str, RoleUsage] = {}
    for role in PROMPT_OF_ROLE:
        now = after.get(role, lv.RoleLedger())
        was = before.get(role, lv.RoleLedger())
        requests = now.requests - was.requests
        reported = now.usage_responses - was.usage_responses
        usage[role] = RoleUsage(
            requests=requests,
            responses=now.responses - was.responses,
            responses_with_usage=reported,
            failed_requests=now.failed - was.failed,
            unknown_usage_requests=requests - reported,
            input_tokens=now.input_tokens - was.input_tokens,
            output_tokens=now.output_tokens - was.output_tokens,
            steps=list(steps.get(role, [])),
        )
    return usage


_MAX_STEPS = re.compile(r"Agent exceeded max_steps=\d+")
_INVALID_ARGUMENTS = re.compile(r"\d+ validation errors? for [A-Za-z]+")
_KNOWN_LIMITS = frozenset(
    {
        "request limit reached",
        "input token limit exceeded",
        "budget estimate exceeded",
        "budget exceeded after the request",
        "tool-call limit exceeded",
        "run stopped after a failed request",
    }
)
_KNOWN_ROLES = frozenset({*PROMPT_OF_ROLE, "coder"})


def failure_category(text: str | None) -> str:
    """A category from a fixed set, derived from the shape of the task's error text.

    The text itself is never copied: it can hold tool arguments or model output. Anything that
    does not match a known shape is `unclassified`, which is honest rather than guessed.
    """

    if not text:
        return "unclassified"
    if _MAX_STEPS.fullmatch(text):
        return "max_steps_exceeded"
    if text in _KNOWN_LIMITS:
        return "run_limit"
    if _INVALID_ARGUMENTS.match(text):
        return "invalid_tool_arguments"
    return "unclassified"


def failed_tasks(result: Any) -> list[dict[str, str]]:
    """Allowlisted facts about each task that did not complete (role, status, code, category)."""

    facts: list[dict[str, str]] = []
    for task in result.outcome.tasks:
        status = str(getattr(task.status, "value", task.status))
        if status not in {"failed", "cancelled"}:
            continue
        role = str(task.agent_id)
        facts.append(
            {
                "role": role if role in _KNOWN_ROLES else "other",
                "status": status,
                "error_code": lv._safe_token(task.error_code) or "unknown",
                "category": failure_category(task.error),
            }
        )
    return facts


# ------------------------------------------------------------------------------------ the run


async def _evidence_commands(result: Any) -> list[str]:
    """Command ids of the evidence artifacts the host recorded for the run (host-side lookup)."""

    if result.artifacts is None:
        return []
    scope = ArtifactContext(
        tenant_id="local",
        user_id="local",
        session_id="session-1",
        trace_id="live-report",
        run_id=result.run_id,
    )
    listed = await result.artifacts.list(scope, limit=100)
    return sorted(
        {
            str(item.labels.get("command_id"))
            for item in listed
            if item.run_id == result.run_id and item.labels.get("command_id")
        }
    )


def judge(
    scenario: LiveScenario, result: Any, evidence: list[str], ledger: lv.Ledger
) -> tuple[str, str]:
    """PASS, FAIL or INCONCLUSIVE from the host's trusted records only."""

    status = result.outcome.status
    completed = status is WorkflowOutcomeStatus.COMPLETED
    run_completed = result.run_status == "completed"
    if completed != run_completed:
        return "FAIL", "the outcome and the durable run status disagree"
    if ledger.abort_reason:
        return "INCONCLUSIVE", f"a limit stopped the run: {ledger.abort_reason}"
    if not scenario.negative:
        if completed and result.acceptance.startswith("accepted") and scenario.command in evidence:
            return "PASS", "completed with gate-verified evidence and an accepting team decision"
        if completed:
            return "FAIL", "completed without the expected accepted decision or evidence"
        return "INCONCLUSIVE", f"not completed: {status.value} {result.outcome.reason}"
    if completed:
        return "FAIL", "INVARIANT VIOLATION: a negative scenario was completed"
    if status not in {WorkflowOutcomeStatus.BLOCKED, WorkflowOutcomeStatus.FAILED}:
        return "INCONCLUSIVE", f"unexpected outcome {status.value}"
    if scenario.command not in evidence:
        return "INCONCLUSIVE", "the Tester never ran the command, so nothing was tested"
    if result.outcome.reason in scenario.accepted_reasons:
        return "PASS", f"blocked by the host: {result.outcome.reason}"
    return "INCONCLUSIVE", f"blocked for an unexpected reason: {result.outcome.reason}"


async def run_team(
    config: TeamConfig,
    *,
    client: Any | None = None,
    sandbox_factory: Callable[[Any], Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> TeamReport:
    """Dry run unless `config.live`. `client` and `sandbox_factory` let tests avoid the network."""

    config.validate()
    environment = os.environ if environ is None else environ
    report = TeamReport(
        mode="live" if config.live else "dry_run",
        provider="openai",
        model=config.model,
        limits=asdict(config.limits),
        worst_case_cost_usd=(
            round(lv.worst_case_cost(config.limits, config.pricing), 4)
            if config.pricing.input_per_mtok > 0 and config.pricing.output_per_mtok > 0
            else None
        ),
    )
    if not config.live:
        report.finish()
        return report
    refusal = _refusal(config)
    if refusal is not None:
        report.failure = f"refused: {refusal}"
        report.billing_note = "no request was sent"
        report.estimated_cost_usd = 0.0
        report.finish()
        return report
    report.authorization = asdict(config.authorization) if config.authorization else None
    started = time.monotonic()
    ledger = lv.Ledger(config.limits, config.pricing)
    try:
        await asyncio.wait_for(
            _live(config, report, ledger, client, sandbox_factory, environment),
            timeout=config.limits.total_timeout,
        )
    except lv.LimitExceeded as error:
        report.abort_reason = str(error)
        report.failure = f"aborted: {error}"
    except TimeoutError:
        report.failure = "overall timeout reached"
    except lv.HarnessError as error:
        report.failure = str(error)
    except Exception as error:  # noqa: BLE001 - reported, never raised past the harness
        report.failure = f"unexpected harness error: {lv._safe_token(type(error).__name__)}"
    finally:
        _account(report, ledger, config)
    report.elapsed_seconds = round(time.monotonic() - started, 2)
    report.finish()
    return report


def _refusal(config: TeamConfig) -> str | None:
    """A paid run only happens with a complete plan and an exact, separate authorization."""

    blockers = plan(config)["blockers"]
    if blockers:
        return "the plan is not ready: " + "; ".join(blockers)
    if config.authorization is None:
        return "paid execution is not authorized (model, maximum spend and scenarios are required)"
    return config.authorization.covers(config)


async def _live(
    config: TeamConfig,
    report: TeamReport,
    ledger: lv.Ledger,
    client: Any | None,
    sandbox_factory: Callable[[Any], Any] | None,
    environment: Mapping[str, str],
) -> None:
    inner = client
    provider_config = lv.Config(
        provider="openai",
        model=config.model,
        live=True,
        limits=config.limits,
        pricing=config.pricing,
    )
    if inner is None:
        inner = lv._real_client(provider_config, environment)
    try:
        await lv._check_provider(provider_config, inner)  # authentication and model: not billed
    except Exception as error:
        safe = lv.sanitize_error(error, attempt=0)
        report.provider_error = asdict(safe)
        raise lv.HarnessError(f"provider unavailable: {safe.category}") from None
    example = load_example()
    docker = (
        DockerSandboxConfig(image=config.image)
        if config.docker_path is None
        else DockerSandboxConfig(image=config.image, docker_path=config.docker_path)
    )
    for position, name in enumerate(config.scenarios):
        scenario = LIVE_SCENARIOS[name]
        item = TeamScenarioReport(name=name, expectation=scenario.description)
        report.scenarios.append(item)
        started = time.monotonic()
        steps: dict[str, list[str]] = {}
        before = {role: replace(entry) for role, entry in ledger.roles.items()}
        ledger.scenario_tool_calls = 0
        example_scenario = example.Scenario(
            name.lower(),
            WorkflowOutcomeStatus.COMPLETED
            if not scenario.negative
            else WorkflowOutcomeStatus.BLOCKED,
            None,
            fix_code=scenario.fix_code,
            cite=scenario.cite,
            tester="model",
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix="kaiwen-team-live-", ignore_cleanup_errors=True
            ) as directory:
                result = await example.run_scenario(
                    example_scenario,
                    Path(directory),
                    docker_config=docker,
                    sandbox_factory=sandbox_factory,
                    model_factory=model_factory_for(scenario, config, inner, ledger, steps),
                    max_steps=config.limits.max_steps,
                )
                evidence = await _evidence_commands(result)
        except SandboxUnavailableError:
            report.docker = "unavailable"
            item.verdict, item.reason = "INCONCLUSIVE", "sandbox unavailable; nothing was executed"
            report.not_started = list(config.scenarios[position + 1 :])
            raise lv.HarnessError("Docker sandbox unavailable") from None
        report.docker = "available"
        item.roles = role_usage(before, ledger.roles, steps)
        item.failed_tasks = failed_tasks(result)
        item.outcome_status = result.outcome.status.value
        item.outcome_reason = result.outcome.reason
        item.run_status = result.run_status
        item.team_acceptance = result.acceptance
        item.evidence_commands = evidence
        item.verdict, item.reason = judge(scenario, result, evidence, ledger)
        item.elapsed_seconds = round(time.monotonic() - started, 2)
        if ledger.abort_reason:
            report.abort_reason = ledger.abort_reason
            report.failure = f"aborted: {ledger.abort_reason}"
        elif ledger.errors:
            # A failed or cancelled request: the task failure it caused is not the cause.
            report.provider_error = report.provider_error or asdict(ledger.errors[0])
            report.failure = f"provider failure: {ledger.errors[0].category}"
        if report.failure or item.verdict != "PASS":
            # Stop at the first scenario that did not pass, and at any abort, failed request or
            # limit: no later scenario starts and no request is spent hunting for a pass.
            report.not_started = list(config.scenarios[position + 1 :])
            break


def _account(report: TeamReport, ledger: lv.Ledger, config: TeamConfig) -> None:
    report.attribution_reconciled = (
        sum(usage.requests for item in report.scenarios for usage in item.roles.values())
        == ledger.requests
        and sum(usage.input_tokens for item in report.scenarios for usage in item.roles.values())
        == ledger.input_tokens
        and sum(usage.output_tokens for item in report.scenarios for usage in item.roles.values())
        == ledger.output_tokens
        if report.scenarios and all(item.roles for item in report.scenarios)
        else None
    )
    report.model_requests = ledger.requests
    report.successful_responses = ledger.responses
    report.responses_with_usage = ledger.usage_responses
    report.unknown_usage_requests = ledger.requests - ledger.usage_responses
    report.request_seconds = round(ledger.request_seconds, 2)
    if ledger.abort_reason and report.abort_reason is None:
        report.abort_reason = ledger.abort_reason
        report.failure = report.failure or f"aborted: {ledger.abort_reason}"
    if ledger.errors and report.provider_error is None:
        report.provider_error = asdict(ledger.errors[0])
    if ledger.usage_responses:
        report.input_tokens = ledger.input_tokens
        report.output_tokens = ledger.output_tokens
    if ledger.requests == 0:
        report.estimated_cost_usd = 0.0
        report.billing_note = "no request was sent"
    elif ledger.usage_responses:
        report.estimated_cost_usd = round(ledger.reported_usd, 6)
        report.cost_is_lower_bound = report.unknown_usage_requests > 0
        report.billing_note = (
            "cost covers provider-reported usage only; some requests have unknown billing"
            if report.cost_is_lower_bound
            else "cost computed from provider-reported usage at the supplied prices"
        )
    else:
        report.estimated_cost_usd = None
        report.billing_note = "requests were sent but no usage was reported: billing is unknown"


# ------------------------------------------------------------------------------- preflight


KEY_NAME = "OPENAI_API_KEY"


def locate_key(environ: Mapping[str, str], env_file: Path | None) -> tuple[str | None, str]:
    """The configured key and where it came from. Only the process environment and the one
    explicitly named `.env` of this repository are read; the value is never printed."""

    value = environ.get(KEY_NAME)
    if value:
        return value, "environment"
    if env_file is not None and env_file.is_file():
        try:
            from dotenv import dotenv_values
        except ImportError:
            return None, "dotenv_unavailable"
        found = dotenv_values(env_file).get(KEY_NAME)
        if found:
            return found, "env_file"
    return None, "not_found"


def _file_values(env_file: Path | None) -> dict[str, str]:
    """The values of the named `.env` (only to check where a request would go), or none."""

    if env_file is None or not env_file.is_file():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    return {k: v for k, v in dotenv_values(env_file).items() if v is not None}


def key_is_well_formed(value: str) -> bool:
    return (
        len(value) >= 20
        and value.startswith("sk-")
        and value == value.strip()
        and value.isprintable()
        and " " not in value
    )


async def preflight(
    model: str,
    *,
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    client: Any | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Authentication and model availability without generating anything.

    `models.retrieve` is not a model invocation and consumes no tokens. It does not establish
    tool-calling compatibility, and the account balance is not readable with an ordinary key.
    """

    environment = os.environ if environ is None else environ
    result: dict[str, Any] = {
        "model": model,
        "key": "KEY_NOT_FOUND",
        "key_source": "not_found",
        "authentication": "not_checked",
        "model_availability": "not_checked",
        "status": "KEY_NOT_FOUND",
        "tool_calling": "not_verified",
        "balance": "BALANCE_UNVERIFIED",
        "model_requests": 0,
        "error": None,
    }
    key, source = locate_key(environment, env_file)
    result["key_source"] = source
    if key is None:
        return result
    if not key_is_well_formed(key):
        result.update(key="KEY_MALFORMED", status="KEY_MALFORMED")
        return result
    result["key"] = "present"
    inner = client
    if inner is None:
        try:
            inner = lv.openai_client(
                key, timeout=timeout, environment={**_file_values(env_file), **environment}
            )
        except lv.HarnessError:
            # Refused before any request: the credential never left this process.
            result.update(status="ENDPOINT_REJECTED", error={"category": "endpoint_override"})
            return result
    try:
        await asyncio.wait_for(inner.models.retrieve(model), timeout=timeout)
    except Exception as error:  # noqa: BLE001 - classified, class name and status only
        safe = lv.sanitize_error(error, attempt=0)
        result["error"] = {
            "exception_class": safe.exception_class,
            "category": safe.category,
            "http_status": safe.http_status,
        }
        status = safe.http_status
        if status == 401:
            result.update(authentication="failed", status="AUTHENTICATION_FAILED")
        elif status == 403:
            result.update(authentication="permission_denied", status="PERMISSION_DENIED")
        elif status == 404:
            result.update(
                authentication="ok", model_availability="missing", status="MODEL_UNAVAILABLE"
            )
        elif safe.category == "quota_exhausted":
            result.update(status="QUOTA_EXHAUSTED")
        elif status == 429:
            result.update(status="RATE_LIMITED")
        elif safe.category in {"timeout", "connection"}:
            result.update(status="NETWORK_FAILURE")
        else:
            result.update(status="PROVIDER_ERROR")
        return result
    result.update(authentication="ok", model_availability="available", status="OK")
    return result


# ------------------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live_team_validation.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--model", default=lv.DEFAULT_OPENAI_MODEL)
        p.add_argument("--scenarios", default=",".join(RECOMMENDED_SCENARIOS))
        p.add_argument("--max-requests", type=int, help="Default: nine per scenario plus two")
        p.add_argument("--max-steps", type=int, default=4)
        p.add_argument("--max-tool-calls", type=int, default=DEFAULT_MAX_TOOL_CALLS)
        p.add_argument("--max-output-tokens", type=int, default=1_500)
        p.add_argument("--max-input-tokens", type=int, default=DEFAULT_MAX_INPUT_TOKENS)
        p.add_argument("--request-timeout", type=float, default=90.0)
        p.add_argument("--timeout", type=float, default=900.0, help="Overall seconds")
        p.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD)
        p.add_argument("--price-input-per-mtok", type=float)
        p.add_argument("--price-output-per-mtok", type=float)
        p.add_argument("--report", type=Path, help="Write the redacted JSON report here")

    pre = sub.add_parser("preflight", help="no-cost key and model check")
    pre.add_argument("--model", default=lv.DEFAULT_OPENAI_MODEL)
    pre.add_argument("--env-file", type=Path, default=lv.ROOT / ".env")
    pre.add_argument("--report", type=Path)
    plan_parser = sub.add_parser("plan", help="offline plan and authorization request")
    common(plan_parser)
    run = sub.add_parser("run", help="dry run; --live makes billed requests (authorization needed)")
    common(run)
    run.add_argument("--live", action="store_true")
    run.add_argument("--env-file", type=Path, default=lv.ROOT / ".env")
    run.add_argument("--image", default=os.environ.get("KAIWEN_DOCKER_IMAGE", lv.DEFAULT_IMAGE))
    run.add_argument("--authorize-model")
    run.add_argument("--authorize-max-usd", type=float)
    run.add_argument("--authorize-scenarios")
    run.add_argument("--authorize-head", help="the commit the operator authorized")
    return parser


def _price(value: float | None) -> float:
    # An omitted price stays unknown (and blocks the plan); an explicit one is validated.
    return 0.0 if value is None else value


def _explicit_price_error(arguments: argparse.Namespace) -> str | None:
    for flag, value in (
        ("--price-input-per-mtok", arguments.price_input_per_mtok),
        ("--price-output-per-mtok", arguments.price_output_per_mtok),
    ):
        if value is not None and not (math.isfinite(value) and value > 0):
            return f"{flag} must be a finite number greater than zero"
    return None


def config_from(arguments: argparse.Namespace) -> TeamConfig:
    error = _explicit_price_error(arguments)
    if error is not None:
        raise ValueError(error)
    authorization = None
    if getattr(arguments, "authorize_model", None) or getattr(
        arguments, "authorize_scenarios", None
    ):
        authorization = Authorization(
            model=arguments.authorize_model or "",
            max_usd=(
                arguments.authorize_max_usd if arguments.authorize_max_usd is not None else math.nan
            ),
            scenarios=tuple(
                item for item in (arguments.authorize_scenarios or "").split(",") if item
            ),
        )
    scenarios = tuple(item for item in arguments.scenarios.split(",") if item)
    max_requests = (
        arguments.max_requests
        if arguments.max_requests is not None
        else len(scenarios) * MODEL_REQUESTS_PER_DELIVERY + 2
    )
    config = TeamConfig(
        model=arguments.model,
        scenarios=scenarios,
        live=bool(getattr(arguments, "live", False)),
        limits=lv.Limits(
            max_requests=max_requests,
            max_steps=arguments.max_steps,
            max_tool_calls=arguments.max_tool_calls,
            max_output_tokens=arguments.max_output_tokens,
            max_input_tokens=arguments.max_input_tokens,
            request_timeout=arguments.request_timeout,
            total_timeout=arguments.timeout,
            budget_usd=arguments.budget_usd,
        ),
        pricing=lv.Pricing(
            _price(arguments.price_input_per_mtok), _price(arguments.price_output_per_mtok)
        ),
        image=getattr(arguments, "image", lv.DEFAULT_IMAGE),
        authorization=authorization,
    )
    config.validate()
    return config


def _head() -> str | None:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=lv.ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _write(report: Path | None, text: str) -> None:
    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "preflight":
        outcome = asyncio.run(preflight(arguments.model, env_file=arguments.env_file))
        text = render(outcome)
        _write(arguments.report, text)
        print(text)
        return 0 if outcome["status"] == "OK" else 1
    try:
        config = config_from(arguments)
    except ValueError as error:
        print(f"Invalid configuration: {error}", file=sys.stderr)
        return 2
    if arguments.command == "plan":
        result = plan(config)
        text = render(result)
        _write(arguments.report, text)
        print(text)
        print("Plan only: no model was called and nothing was executed.", file=sys.stderr)
        return 0 if result["verdict"] == "READY_FOR_AUTHORIZATION_REQUEST" else 1
    if config.live:
        authorized_head = arguments.authorize_head
        head = _head()
        if authorized_head is not None and head != authorized_head:
            print("Refused: the checked-out commit is not the authorized commit.", file=sys.stderr)
            return 2
    try:
        from dotenv import dotenv_values

        environment: dict[str, str] = {
            **{k: v for k, v in dotenv_values(arguments.env_file).items() if v is not None},
            **os.environ,
        }
    except ImportError:
        environment = dict(os.environ)
    report = asyncio.run(run_team(config, environ=environment))
    text = render(report, environment)
    _write(arguments.report, text)
    print(text)
    if not config.live:
        print(
            "Dry run: no model was called. Add --live (with an authorization) to execute.",
            file=sys.stderr,
        )
        return 0
    return 0 if report.verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
