"""Opt-in live validation: a real model as Tester through the existing worker stack.

This is a validation harness, not a framework. It composes the existing pieces and adds only
guards around the provider client: `AgentRuntimeWorker`, `TaskScheduler`,
`WorkflowController`, the Docker sandbox, the signed evidence pipeline and the completion
gate. A real model is asked to call the authorized `testing.run` tool on a disposable
project. The host then checks, independently of anything the model says, whether a sandbox
command ran, whether the evidence authenticates, and whether the gate passes.

Nothing external runs without `--live`. Without it the command prints its configuration and
the worst-case spend estimate and exits. API keys come from the environment (or the ignored
repository `.env`), are never printed, stored, or placed in prompts, and the report is scanned
for them before it is written.

Money: the budget check is a local pre-request estimate plus post-request accounting from
provider-reported usage. It stops this process; it cannot enforce an exact monetary ceiling.
Only a spend limit set on the provider account can do that.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kaiwen_agent.agents import VersionedReference
from kaiwen_agent.artifacts import ArtifactContext, ArtifactManager
from kaiwen_agent.audit import SecretRedactor
from kaiwen_agent.control import RunControl
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.kits.coding import (
    AuthorizedCodingWorkspace,
    EvidenceBinding,
    EvidenceSigner,
    TrustedEvidencePipeline,
    WorkspaceFingerprintProvider,
)
from kaiwen_agent.kits.coding.prompting import install_coding_prompts
from kaiwen_agent.kits.coding.standards import coding_standards_packs
from kaiwen_agent.models import OpenAIResponsesProvider
from kaiwen_agent.models.ollama import OllamaChatProvider
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.prompts import PromptRegistry
from kaiwen_agent.quality import CompletionGate, VerifierRegistry
from kaiwen_agent.runtime import (
    AgentRuntime,
    AutonomyLevel,
    DurableRunManager,
    RuntimeIdentity,
    RuntimeVersionPins,
)
from kaiwen_agent.standards import StandardsRegistry, StandardsResolver
from kaiwen_agent.tasks import TaskScheduler
from kaiwen_agent.toolkits import (
    CommandCatalog,
    CommandPreset,
    DockerSandboxConfig,
    DockerSandboxRunner,
    SandboxUnavailableError,
    SnapshotBuilder,
    testing_tools,
)
from kaiwen_agent.tools import ToolExecutor
from kaiwen_agent.workers import WorkerRegistry
from kaiwen_agent.workflow import (
    AgentRuntimeWorker,
    WorkflowCompiler,
    WorkflowController,
    WorkflowOutcomeStatus,
    WorkflowPlan,
    WorkflowTaskSpec,
)

ROOT = Path(__file__).parents[1]
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:7b"
DEFAULT_IMAGE = "python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7"
# Assumed list prices in USD per million tokens. They are deliberately generous placeholders,
# not a statement of the provider's current price: override them with --price-* after
# checking the pricing page. Local models cost nothing.
ASSUMED_OPENAI_PRICE = (2.50, 20.00)
WORKING = "def add(a, b):\n    return a + b\n"
BROKEN = "def add(a, b):\n    return a - b\n"
TASK_TEXT = (
    "Run the project's unit tests exactly once by calling the testing.run tool with "
    "command_id 'unit.tests'. Then report in one or two sentences what the tool actually "
    "returned. Do not claim any result you did not observe."
)


class LimitExceeded(RuntimeError):
    """A configured live-validation limit was reached; the run is aborted."""


class HarnessError(RuntimeError):
    """A harness-authored message that is safe to put in the report verbatim.

    Any other exception is reported by class name only: its text may carry provider payloads
    or secrets.
    """


_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


_SECRET_SHAPE = re.compile(r"(^sk-|[A-Za-z0-9]{32,})")


def _safe_token(value: Any) -> str | None:
    """Short identifier-like strings only; anything shaped like a key is dropped."""

    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return None
    return None if _SECRET_SHAPE.search(value) else value


def _safe_status(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 100 <= value <= 599 else None


@dataclass(frozen=True, slots=True)
class SafeError:
    """Allowlisted facts about one provider failure. No message, body, header or payload."""

    exception_class: str | None
    category: str
    stage: str
    attempt: int
    http_status: int | None = None
    error_code: str | None = None
    error_param: str | None = None
    error_type: str | None = None
    http_response_observed: bool = False
    model_response_received: bool = False
    usage_available: bool = False


def sanitize_error(
    error: BaseException,
    *,
    attempt: int,
    model_response_received: bool = False,
    usage_available: bool = False,
) -> SafeError:
    """Classify a failure from structured SDK attributes only, never from its text."""

    names = {item.__name__ for item in type(error).__mro__}
    status = _safe_status(getattr(error, "status_code", None))
    if status is None:
        status = _safe_status(getattr(getattr(error, "response", None), "status_code", None))
    code = _safe_token(getattr(error, "code", None))
    if "APITimeoutError" in names or "TimeoutException" in names or isinstance(error, TimeoutError):
        category, stage = "timeout", "transport_failure"
    elif names & {"APIConnectionError", "ConnectError", "NetworkError", "TransportError"}:
        category, stage = "connection", "transport_failure"
    elif status is not None:
        stage = "http_error"
        known = {
            400: "invalid_request",
            401: "authentication",
            403: "permission_denied",
            404: "not_found",
            422: "invalid_request",
        }
        if status == 429:
            category = "quota_exhausted" if code == "insufficient_quota" else "rate_limited"
        else:
            category = known.get(status) or ("server_error" if status >= 500 else "http_error")
    elif names & {"ValidationError", "TypeError", "ValueError"}:
        category, stage = "sdk_validation", "rejected_before_transport"
    else:
        category, stage = "unclassified", "undetermined"
    return SafeError(
        exception_class=_safe_token(type(error).__name__),
        category=category,
        stage=stage,
        attempt=attempt,
        http_status=status,
        error_code=code,
        error_param=_safe_token(getattr(error, "param", None)),
        error_type=_safe_token(getattr(error, "type", None)),
        http_response_observed=status is not None,
        model_response_received=model_response_received,
        usage_available=usage_available,
    )


@dataclass(frozen=True, slots=True)
class Limits:
    max_requests: int = 4
    max_steps: int = 4
    max_tool_calls: int = 3
    max_output_tokens: int = 1_500
    max_input_tokens: int = 6_000
    request_timeout: float = 90.0
    total_timeout: float = 600.0
    budget_usd: float = 0.20

    def validate(self) -> None:
        if min(self.max_requests, self.max_steps, self.max_tool_calls) < 1:
            raise ValueError("request, step and tool-call limits must be at least 1")
        if min(self.max_output_tokens, self.max_input_tokens) < 1:
            raise ValueError("token limits must be at least 1")
        if min(self.request_timeout, self.total_timeout) <= 0 or self.budget_usd < 0:
            raise ValueError("timeouts must be positive and the budget cannot be negative")


@dataclass(frozen=True, slots=True)
class Pricing:
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1e6


def worst_case_cost(limits: Limits, pricing: Pricing) -> float:
    """Upper bound for the whole run under the limits and the assumed prices."""

    return limits.max_requests * pricing.cost(limits.max_input_tokens, limits.max_output_tokens)


@dataclass(slots=True)
class Ledger:
    """Local usage accounting shared by every request of one manually triggered run."""

    limits: Limits
    pricing: Pricing
    # `requests` counts attempts that reached the transport; retries are disabled, so it is
    # also the number of billable-at-most requests.
    requests: int = 0
    responses: int = 0
    usage_responses: int = 0
    # Provider-reported usage only.
    input_tokens: int = 0
    output_tokens: int = 0
    reported_usd: float = 0.0
    # Budget accounting: reported cost, plus the pessimistic pre-request estimate for any
    # attempt whose usage is unknown (failed or usage-less), so unknown is never treated as zero.
    spent_usd: float = 0.0
    scenario_tool_calls: int = 0
    abort_reason: str | None = None
    request_seconds: float = 0.0
    guard_failures: int = 0
    errors: list[SafeError] = field(default_factory=list)
    _pending: float = 0.0

    def before(self, estimated_input_tokens: int) -> None:
        if self.requests >= self.limits.max_requests:
            self._abort("request limit reached")
        if estimated_input_tokens > self.limits.max_input_tokens:
            self._abort("input token limit exceeded")
        projected = self.spent_usd + self.pricing.cost(
            estimated_input_tokens, self.limits.max_output_tokens
        )
        if projected > self.limits.budget_usd:
            self._abort("budget estimate exceeded")
        self.requests += 1
        self._pending = self.pricing.cost(estimated_input_tokens, self.limits.max_output_tokens)

    def record_failure(self, error: BaseException) -> None:
        """A transport or HTTP failure at the guarded client; usage is unknown."""

        self.guard_failures += 1
        self.spent_usd += self._pending
        if len(self.errors) < 5:
            self.errors.append(sanitize_error(error, attempt=self.requests))

    def note_exception(self, error: BaseException, *, attempted: bool, received: bool) -> None:
        """A failure outside the client call: before transport, or after a good response."""

        if len(self.errors) >= 5:
            return
        base = sanitize_error(error, attempt=self.requests, model_response_received=received)
        if received:
            category, stage = "adapter_failure", "adapter_failure_after_response"
        elif attempted:
            category, stage = base.category, base.stage
        else:
            category, stage = "adapter_error", "rejected_before_transport"
        self.errors.append(
            SafeError(
                **{
                    **{f: getattr(base, f) for f in SafeError.__slots__},
                    "category": category,
                    "stage": stage,
                }
            )
        )

    def after(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        tool_calls: int,
        seconds: float,
    ) -> None:
        self.responses += 1
        if input_tokens is None or output_tokens is None:
            self.spent_usd += self._pending
        else:
            cost = self.pricing.cost(input_tokens, output_tokens)
            self.usage_responses += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.reported_usd += cost
            self.spent_usd += cost
        self.request_seconds += seconds
        self.scenario_tool_calls += tool_calls
        if self.scenario_tool_calls > self.limits.max_tool_calls:
            self._abort("tool-call limit exceeded")
        if self.spent_usd > self.limits.budget_usd:
            self._abort("budget exceeded after the request")

    def _abort(self, reason: str) -> None:
        self.abort_reason = reason
        raise LimitExceeded(reason)


def estimate_tokens(request: Mapping[str, Any]) -> int:
    """A deliberately pessimistic size estimate (three characters per token)."""

    return len(json.dumps(request, default=str, ensure_ascii=False)) // 3 + 1


class _GuardedResponses:
    def __init__(self, inner: Any, ledger: Ledger) -> None:
        self._inner = inner
        self._ledger = ledger

    async def create(self, **request: Any) -> Any:
        request["store"] = False
        request["max_output_tokens"] = self._ledger.limits.max_output_tokens
        self._ledger.before(estimate_tokens(request))
        started = time.monotonic()
        try:
            response = await self._inner.responses.create(**request)
        except Exception as error:
            self._ledger.record_failure(error)
            raise
        usage = getattr(response, "usage", None)
        calls = sum(
            1
            for item in getattr(response, "output", [])
            if getattr(item, "type", None) == "function_call"
        )
        self._ledger.after(
            _optional_count(getattr(usage, "input_tokens", None)),
            _optional_count(getattr(usage, "output_tokens", None)),
            calls,
            time.monotonic() - started,
        )
        return response


class GuardedOpenAIClient:
    """The `responses.create` surface the provider uses, with limits and accounting."""

    def __init__(self, inner: Any, ledger: Ledger) -> None:
        self.responses = _GuardedResponses(inner, ledger)


class GuardedOllamaClient:
    """The `chat` surface the provider uses, with limits and accounting."""

    def __init__(self, inner: Any, ledger: Ledger, *, context_tokens: int = 8_192) -> None:
        self._inner = inner
        self._ledger = ledger
        self._context_tokens = context_tokens
        self.eval_seconds = 0.0
        self.eval_tokens = 0

    async def chat(self, **request: Any) -> Any:
        request["options"] = {
            "num_predict": self._ledger.limits.max_output_tokens,
            "num_ctx": self._context_tokens,
            "temperature": 0,
        }
        self._ledger.before(estimate_tokens(request))
        started = time.monotonic()
        try:
            response = await self._inner.chat(**request)
        except Exception as error:
            self._ledger.record_failure(error)
            raise
        raw = _as_mapping(response)
        message = _as_mapping(raw.get("message", {}))
        calls = len(message.get("tool_calls") or [])
        output_tokens = _optional_count(raw.get("eval_count"))
        self.eval_tokens += output_tokens or 0
        self.eval_seconds += _count(raw.get("eval_duration")) / 1e9
        self._ledger.after(
            _optional_count(raw.get("prompt_eval_count")),
            output_tokens,
            calls,
            time.monotonic() - started,
        )
        return response


class DiagnosticProvider:
    """Wraps any provider so a failure outside the client call is still attributed.

    The guarded client sees transport and HTTP failures. This wrapper sees everything else the
    adapter raises, and records only whether a request was attempted and whether a response
    had been received, plus the exception class name.
    """

    def __init__(self, inner: Any, ledger: Ledger) -> None:
        self._inner = inner
        self._ledger = ledger

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def respond(self, *args: Any, **kwargs: Any) -> Any:
        ledger = self._ledger
        attempts, failures, responses = ledger.requests, ledger.guard_failures, ledger.responses
        try:
            return await self._inner.respond(*args, **kwargs)
        except LimitExceeded:
            raise
        except Exception as error:
            if ledger.guard_failures == failures:
                ledger.note_exception(
                    error,
                    attempted=ledger.requests > attempts,
                    received=ledger.responses > responses,
                )
            raise


def _optional_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump(mode="json", exclude_none=True)
        if isinstance(dumped, Mapping):
            return dumped
    return {}


@dataclass(slots=True)
class Stages:
    """What succeeded, kept separate. `not_run` is never a pass."""

    provider_connection: str = "not_run"
    model_tool_calling: str = "not_run"
    tool_execution: str = "not_run"
    evidence_authentication: str = "not_run"
    completion_gate: str = "not_run"
    overall: str = "not_run"


@dataclass(slots=True)
class ScenarioReport:
    name: str
    expectation: str
    stages: Stages = field(default_factory=Stages)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_errors: int = 0
    sandbox_executed: bool = False
    sandbox_exit_code: int | None = None
    snapshot_fingerprint: str | None = None
    gate_status: str | None = None
    gate_reason: str | None = None
    worker_output: str = "unavailable"
    worker_verified_field: bool | None = None
    model_claimed_success: bool | None = None
    task_status: str | None = None
    task_error_code: str | None = None
    failed_stage: str | None = None
    failure_category: str | None = None
    reason: str | None = None
    elapsed_seconds: float = 0.0


@dataclass(slots=True)
class Report:
    provider: str
    model: str
    prompt: str
    mode: str
    docker: str = "not_checked"
    limits: dict[str, Any] = field(default_factory=dict)
    scenarios: list[ScenarioReport] = field(default_factory=list)
    # Attempts that reached the transport (retries are disabled), successful responses, and
    # how many of those carried provider-reported usage. Tokens and cost cover reported usage
    # only and are null when no response reported any; attempts without usage have unknown
    # billing, which is never presented as zero.
    model_requests: int = 0
    successful_responses: int = 0
    responses_with_usage: int = 0
    unknown_usage_requests: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    cost_is_lower_bound: bool = False
    billing_note: str | None = None
    provider_error: dict[str, Any] | None = None
    request_compatibility_warnings: list[str] = field(default_factory=list)
    worst_case_cost_usd: float | None = None
    request_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    hardware: dict[str, Any] = field(default_factory=dict)
    abort_reason: str | None = None
    failure: str | None = None
    verdict: str = "NOT_RUN"

    def finish(self) -> None:
        if self.mode != "live":
            self.verdict = "NOT_RUN"
        elif self.failure is not None or not self.scenarios:
            self.verdict = "FAIL"
        else:
            self.verdict = (
                "PASS" if all(item.stages.overall == "pass" for item in self.scenarios) else "FAIL"
            )


class _Redaction:
    """Scrubs configured secret values and common key shapes from the final text."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._values = [
            value for name, value in environ.items() if _is_secret_name(name) and len(value) >= 8
        ]
        self._redactor = SecretRedactor()

    def apply(self, text: str) -> str:
        for value in self._values:
            text = text.replace(value, "[REDACTED]")
        scrubbed = self._redactor.redact(text)
        return scrubbed if isinstance(scrubbed, str) else text


def _is_secret_name(name: str) -> bool:
    return re.search(r"(KEY|TOKEN|SECRET|PASSWORD)", name, re.IGNORECASE) is not None


def render_report(report: Report, environ: Mapping[str, str] | None = None) -> str:
    text = json.dumps(asdict(report), indent=2, sort_keys=True)
    return _Redaction(os.environ if environ is None else environ).apply(text)


@dataclass(frozen=True, slots=True)
class Config:
    provider: str
    model: str
    live: bool = False
    prompt: str = "coding.tester"
    scenario: str = "both"
    limits: Limits = field(default_factory=Limits)
    pricing: Pricing = field(default_factory=Pricing)
    ollama_host: str | None = None
    image: str = DEFAULT_IMAGE


RunnerFactory = Callable[[WorkspaceFilesystem], Any]


def default_runner(config: Config) -> RunnerFactory:
    def build(filesystem: WorkspaceFilesystem) -> Any:
        return DockerSandboxRunner(
            DockerSandboxConfig(image=config.image), filesystem=filesystem, root="main"
        )

    return build


def make_project(root: Path, source: str = WORKING) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "tests" / "__init__.py").write_text("")
    (root / "pkg" / "calc.py").write_text(source)
    (root / "tests" / "test_calc.py").write_text(
        "import unittest\nfrom pkg.calc import add\n\n"
        "class AddTests(unittest.TestCase):\n"
        "    # Expected result: add(2, 3) == 5.\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n"
    )


def build_provider(
    config: Config, client: Any, instructions: str
) -> OpenAIResponsesProvider | OllamaChatProvider:
    if config.provider == "openai":
        return OpenAIResponsesProvider(
            model=config.model, instructions=instructions, client=client, store=False
        )
    # Only models that support thinking accept the flag; it is bounded off for the qwen3 family.
    think = False if config.model.startswith("qwen3") else None
    return OllamaChatProvider(
        model=config.model, instructions=instructions, client=client, think=think
    )


def load_instructions(prompt: str) -> tuple[str, int, str]:
    """The registered role prompt text, its version and checksum. Never modified here."""

    registry = PromptRegistry()
    install_coding_prompts(registry)
    definition = registry.get(prompt)
    return definition.template, definition.version, getattr(definition, "checksum", "")


async def run_validation(
    config: Config,
    *,
    client: Any | None = None,
    runner_factory: RunnerFactory | None = None,
    environ: Mapping[str, str] | None = None,
) -> Report:
    """Run the scenarios. `client` and `runner_factory` exist so tests need no network."""

    config.limits.validate()
    environment = os.environ if environ is None else environ
    report = Report(
        provider=config.provider,
        model=config.model,
        prompt=config.prompt,
        mode="live" if config.live else "dry_run",
        limits=asdict(config.limits),
        worst_case_cost_usd=round(worst_case_cost(config.limits, config.pricing), 4),
    )
    if config.provider == "openai":
        report.request_compatibility_warnings = probe_openai_tool_schemas()
    if not config.live:
        report.finish()
        return report
    started = time.monotonic()
    try:
        await asyncio.wait_for(
            _live(config, report, client, runner_factory or default_runner(config), environment),
            timeout=config.limits.total_timeout,
        )
    except LimitExceeded as error:
        report.abort_reason = str(error)
        report.failure = f"aborted: {error}"
    except TimeoutError:
        report.failure = "overall timeout reached"
    except HarnessError as error:
        report.failure = str(error)
    except Exception as error:  # noqa: BLE001 - reported, never raised past the harness
        # Class name only: the text of an arbitrary exception may carry a payload or a secret.
        report.failure = f"unexpected harness error: {_safe_token(type(error).__name__)}"
    if report.billing_note is None:
        # Accounting runs once requests were possible; failing earlier means none was sent.
        report.estimated_cost_usd = 0.0
        report.billing_note = "no request was sent"
    report.elapsed_seconds = round(time.monotonic() - started, 2)
    report.finish()
    return report


async def _live(
    config: Config,
    report: Report,
    client: Any | None,
    runner_factory: RunnerFactory,
    environment: Mapping[str, str],
) -> None:
    ledger = Ledger(config.limits, config.pricing)
    guarded: Any
    inner = client
    if inner is None:
        inner = _real_client(config, environment)
    try:
        await _check_provider(config, inner)
    except Exception as error:
        safe = sanitize_error(error, attempt=0)
        report.provider_error = asdict(safe)
        raise HarnessError(f"provider unavailable: {safe.category}") from None
    guarded = (
        GuardedOpenAIClient(inner, ledger)
        if config.provider == "openai"
        else GuardedOllamaClient(inner, ledger)
    )
    instructions, _, _ = load_instructions(config.prompt)
    names = ("pass", "negative") if config.scenario == "both" else (config.scenario,)
    try:
        with tempfile.TemporaryDirectory(
            prefix="kaiwen-live-", ignore_cleanup_errors=True
        ) as directory:
            for name in names:
                await _scenario(
                    config,
                    report,
                    ledger,
                    guarded,
                    instructions,
                    runner_factory,
                    Path(directory),
                    name,
                )
                if ledger.abort_reason:
                    # The runtime turns a provider exception into a failed task, so the
                    # limit surfaces here from the ledger rather than as an exception.
                    report.abort_reason = ledger.abort_reason
                    report.failure = f"aborted: {ledger.abort_reason}"
                ledger.scenario_tool_calls = 0
                if report.failure:
                    break
    finally:
        if ledger.abort_reason and report.abort_reason is None:
            report.abort_reason = ledger.abort_reason
            report.failure = report.failure or f"aborted: {ledger.abort_reason}"
        _account(report, ledger, config)
        if config.provider == "ollama":
            report.hardware = await _ollama_hardware(inner, guarded, config.model)


def _account(report: Report, ledger: Ledger, config: Config) -> None:
    report.model_requests = ledger.requests
    report.successful_responses = ledger.responses
    report.responses_with_usage = ledger.usage_responses
    report.unknown_usage_requests = ledger.requests - ledger.usage_responses
    report.request_seconds = round(ledger.request_seconds, 2)
    if ledger.errors:
        report.provider_error = asdict(ledger.errors[0])
    if ledger.usage_responses:
        report.input_tokens = ledger.input_tokens
        report.output_tokens = ledger.output_tokens
    if config.provider != "openai":
        report.estimated_cost_usd = 0.0
        report.billing_note = "local model; no provider billing"
        return
    if ledger.requests == 0:
        report.estimated_cost_usd = 0.0
        report.billing_note = "no request was sent"
    elif ledger.usage_responses:
        report.estimated_cost_usd = round(ledger.reported_usd, 6)
        report.cost_is_lower_bound = report.unknown_usage_requests > 0
        report.billing_note = (
            "cost covers provider-reported usage only; some requests have unknown billing"
            if report.cost_is_lower_bound
            else "cost computed from provider-reported usage at assumed prices"
        )
    else:
        report.estimated_cost_usd = None
        report.billing_note = (
            "request(s) were sent but the provider reported no usage: billing is unknown, "
            "check the provider dashboard"
        )


def _real_client(config: Config, environment: Mapping[str, str]) -> Any:
    if config.provider == "openai":
        if not environment.get("OPENAI_API_KEY"):
            raise HarnessError("OPENAI_API_KEY is not configured")
        from openai import AsyncOpenAI

        # No hidden retries: every request is counted by the ledger.
        return AsyncOpenAI(
            api_key=environment["OPENAI_API_KEY"],
            timeout=config.limits.request_timeout,
            max_retries=0,
        )
    try:
        from ollama import AsyncClient  # type: ignore[import-not-found]
    except ImportError:
        return OllamaHttpClient(config.ollama_host, config.limits.request_timeout)
    return AsyncClient(host=config.ollama_host, timeout=config.limits.request_timeout)


class OllamaHttpClient:
    """Minimal transport for the `list`, `ps` and `chat` calls, used when the optional
    `ollama` package is not installed. The existing `OllamaChatProvider` still builds the
    requests and parses the replies; this only moves JSON over HTTP to the local service."""

    def __init__(self, host: str | None, timeout: float) -> None:
        self._base = (host or "http://127.0.0.1:11434").rstrip("/")
        self._timeout = timeout

    async def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.request(method, self._base + path, json=body)
            response.raise_for_status()
            return response.json()

    async def list(self) -> Any:
        return await self._call("GET", "/api/tags")

    async def ps(self) -> Any:
        return await self._call("GET", "/api/ps")

    async def chat(self, **request: Any) -> Any:
        return await self._call("POST", "/api/chat", request)


async def _check_provider(config: Config, client: Any) -> None:
    """Connection and model availability. Neither is a billed model request."""

    if config.provider == "openai":
        await client.models.retrieve(config.model)
        return
    listing = _as_mapping(await client.list())
    names = {
        str(_as_mapping(item).get("model") or _as_mapping(item).get("name"))
        for item in listing.get("models", [])
    }
    wanted = {config.model, f"{config.model}:latest"}
    if not wanted & names:
        raise LookupError("model is not installed locally; this harness never pulls models")


async def _ollama_hardware(inner: Any, guarded: Any, model: str) -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        for item in _as_mapping(await inner.ps()).get("models", []):
            entry = _as_mapping(item)
            if str(entry.get("model") or entry.get("name")) in {model, f"{model}:latest"}:
                size, vram = _count(entry.get("size")), _count(entry.get("size_vram"))
                if size:
                    info["gpu_offload_percent"] = round(100 * vram / size)
    except Exception:  # noqa: BLE001 - telemetry is best effort and never claimed when absent
        pass
    if isinstance(guarded, GuardedOllamaClient) and guarded.eval_seconds > 0:
        info["generation_tokens_per_second"] = round(guarded.eval_tokens / guarded.eval_seconds, 1)
    info.setdefault("note", "gpu_offload_percent is reported by Ollama only when available")
    return info


async def _scenario(
    config: Config,
    report: Report,
    ledger: Ledger,
    client: Any,
    instructions: str,
    runner_factory: RunnerFactory,
    base: Path,
    name: str,
) -> None:
    expectation = (
        "gate passes on executed, signed evidence"
        if name == "pass"
        else ("a failing project is never completed, whatever the model claims")
    )
    outcome_report = ScenarioReport(name=name, expectation=expectation)
    outcome_report.stages.provider_connection = "pass"
    report.scenarios.append(outcome_report)
    started = time.monotonic()
    root = base / f"project-{name}"
    root.mkdir()
    make_project(root, WORKING if name == "pass" else BROKEN)
    filesystem = WorkspaceFilesystem(workspace_id="live", roots={"main": root})
    runner = runner_factory(filesystem)
    try:
        if hasattr(runner, "verify"):
            await runner.verify()
        report.docker = "available"
    except SandboxUnavailableError:
        report.docker = "unavailable"
        outcome_report.reason = "sandbox unavailable; nothing was executed"
        raise HarnessError("Docker sandbox unavailable") from None
    signer = EvidenceSigner(key=os.urandom(32))
    artifacts = ArtifactManager()
    pipeline = TrustedEvidencePipeline.build(
        artifacts=artifacts,
        signer=signer,
        bindings=[
            EvidenceBinding(
                command_id="unit.tests", requirement_id="coding.tests.run", evidence_type="test_run"
            )
        ],
        trusted_runners=[runner],
    )
    registry = VerifierRegistry()
    pipeline.install_verifiers(registry)
    preset = CommandPreset(
        id="unit.tests",
        category="test",
        executable="python",
        arguments=("-m", "unittest", "discover", "-s", "tests", "-t", "."),
        timeout_seconds=120,
    )
    catalog = CommandCatalog(
        filesystem=filesystem,
        root="main",
        runner=runner,
        presets=(preset,),
        observers=(pipeline.recorder,),
    )
    fingerprints = WorkspaceFingerprintProvider(SnapshotBuilder(filesystem, "main"))
    workspace = AuthorizedCodingWorkspace(
        filesystem=filesystem,
        root="main",
        fingerprints=fingerprints,
        catalog=catalog,
        runner=runner,
        pipeline=pipeline,
        identity="live-validation-project",
    )
    store = SQLitePersistence(base / f"state-{name}.sqlite3")
    runs = DurableRunManager(store)
    workers = WorkerRegistry()
    controller = WorkflowController(
        runs=runs,
        compiler=WorkflowCompiler(workers=workers, worker_allowlist=frozenset({"tester"})),
        scheduler_factory=lambda graph: TaskScheduler(
            graph=graph,
            workers=workers,
            store=store,
            control=RunControl(checkpoints=store),
            max_concurrency=1,
        ),
        completion_gate=CompletionGate(verifiers=registry, artifacts=artifacts),
        fingerprint_provider=fingerprints,
        workspace_binding=workspace.binding,
    )
    runtime = AgentRuntime(
        name="tester",
        model=DiagnosticProvider(build_provider(config, client, instructions), ledger),
        tools=testing_tools(catalog, unattended_categories=frozenset({"test"})),
        permissions=frozenset({"tests.run"}),
        tool_executor=ToolExecutor(action_intent_store=store),
        max_steps=config.limits.max_steps,
    )
    worker = AgentRuntimeWorker(
        runtime=runtime,
        runs=runs,
        task_store=store,
        role="tester",
        permissions=frozenset({"tests.run"}),
        tool_names=("testing.run",),
        evidence_tool_names=frozenset({"testing.run"}),
        workspace=workspace,
        controller=controller,
        artifacts=artifacts,
        fingerprints=fingerprints,
        max_summary_chars=1_000,
    )
    workers.register("tester", worker)
    standards = StandardsResolver(StandardsRegistry(coding_standards_packs())).resolve(
        [VersionedReference(id="coding.tests", version=1)],
        condition_facts={"has_executable_tests": True},
    )
    owner = RuntimeIdentity(
        session_id="live", tenant_id="local", user_id="local", trace_id=f"live-{name}"
    )
    run = await runs.create(
        identity=owner,
        autonomy=AutonomyLevel.AUTONOMOUS,
        version_pins=_pins(config),
        workspace_binding=workspace.binding,
    )
    plan = WorkflowPlan(
        objective="Verify the project with its unit tests.",
        tasks=(
            WorkflowTaskSpec(
                id="verify",
                name="Run the unit tests",
                description=TASK_TEXT,
                worker_id="tester",
                input={"objective": TASK_TEXT, "acceptance_criteria": ["Tests were executed."]},
                timeout_seconds=config.limits.total_timeout,
            ),
        ),
    )
    try:
        outcome = await controller.execute(
            run=run,
            identity=owner,
            plan=plan,
            standards=standards,
            protected_input_fingerprint=f"objective:{name}",
        )
    except LimitExceeded:
        raise
    except Exception as error:  # noqa: BLE001
        outcome_report.reason = f"workflow error: {type(error).__name__}"
        outcome_report.stages.overall = "fail"
        report.failure = outcome_report.reason
        return
    finally:
        outcome_report.elapsed_seconds = round(time.monotonic() - started, 2)
    await _assess(outcome_report, outcome, artifacts, signer, run.id, ledger)


def _pins(config: Config) -> RuntimeVersionPins:
    return RuntimeVersionPins(
        workspace_id="live",
        workspace_version=1,
        agent_id="tester",
        agent_version=1,
        prompt_id=config.prompt,
        prompt_version=1,
        prompt_checksum="registered",
        capability_manifest_id="tester",
        capability_manifest_version=1,
        provider_ref=config.provider,
        tool_names=("testing.run",),
        toolset_checksum="testing.run",
    )


async def _assess(
    result: ScenarioReport,
    outcome: Any,
    artifacts: ArtifactManager,
    signer: EvidenceSigner,
    run_id: str,
    ledger: Ledger,
) -> None:
    """Judge from host records only. The model's words never decide a stage."""

    task = outcome.tasks[0] if outcome.tasks else None
    output: dict[str, Any] = (
        task.output if task is not None and isinstance(task.output, dict) else {}
    )
    result.worker_output = "available" if output else "unavailable"
    if task is not None:
        result.task_status = task.status.value
        result.task_error_code = _safe_token(task.error_code)
    calls = [item for item in output.get("tool_calls", []) if isinstance(item, dict)]
    result.tool_calls = calls[:10]
    result.tool_errors = sum(1 for item in calls if item.get("is_error"))
    result.worker_verified_field = output.get("verified") if output else None
    summary = str(output.get("summary", "")).lower()
    result.model_claimed_success = bool(re.search(r"\b(pass|passed|passes|success)", summary))
    result.gate_status = outcome.status.value
    result.gate_reason = outcome.reason
    asked = any(item.get("tool") == "testing.run" for item in calls)
    if asked:
        result.stages.model_tool_calling = "pass"
    elif output and ledger.responses:
        result.stages.model_tool_calling = "fail"

    context = ArtifactContext(
        tenant_id="local", user_id="local", session_id="live", trace_id="live", run_id=run_id
    )
    authenticated: list[dict[str, Any]] = []
    for metadata in await artifacts.list(context, limit=50):
        artifact = await artifacts.read(context, metadata.id)
        if not signer.verify(
            tenant_id=metadata.tenant_id,
            owner_user_id=metadata.owner_user_id,
            run_id=metadata.run_id,
            content=artifact.content,
            labels=metadata.labels,
        ):
            continue
        try:
            payload = json.loads(artifact.content)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("run_id") == run_id:
            authenticated.append(payload)
    result.sandbox_executed = bool(authenticated)
    if authenticated:
        result.stages.tool_execution = "pass"
        result.stages.evidence_authentication = "pass"
    elif asked:
        result.stages.tool_execution = "fail"
        result.stages.evidence_authentication = "fail"
    if authenticated:
        latest = authenticated[-1]
        result.sandbox_exit_code = latest.get("exit_code")
        fingerprint = latest.get("snapshot_fingerprint")
        result.snapshot_fingerprint = fingerprint[:16] if isinstance(fingerprint, str) else None
    completed = outcome.status == WorkflowOutcomeStatus.COMPLETED
    if completed:
        result.stages.completion_gate = "pass"
    elif outcome.quality_result is not None:
        result.stages.completion_gate = "fail"  # the gate ran and did not pass
    if output and result.worker_verified_field is not False:
        result.reason = "worker output claimed verification"
        result.stages.overall = "fail"
    elif not output:
        result.stages.overall = "fail"
    elif result.name == "pass":
        ok = asked and bool(authenticated) and completed and result.sandbox_exit_code == 0
        result.stages.overall = "pass" if ok else "fail"
    else:
        # The model must have run the tests; only then does a block prove anything.
        failed_run = bool(authenticated) and result.sandbox_exit_code not in (0, None)
        ok = asked and failed_run and not completed
        result.stages.overall = "pass" if ok else "fail"
    if result.stages.overall != "pass":
        _classify(result, ledger, completed, asked, bool(authenticated), has_output=bool(output))


def _classify(
    result: ScenarioReport,
    ledger: Ledger,
    completed: bool,
    asked: bool,
    authenticated: bool,
    *,
    has_output: bool,
) -> None:
    """The earliest failing stage that the host's own records confirm; else undetermined."""

    if ledger.abort_reason:
        result.failure_category = result.failed_stage = "limit_exceeded"
        result.reason = f"aborted: {ledger.abort_reason}"
    elif not has_output:
        error = ledger.errors[0] if ledger.errors else None
        if error is not None:
            result.failure_category = error.stage
            result.failed_stage = error.stage
            result.reason = _describe(error)
        else:
            result.failure_category = "undetermined"
            result.failed_stage = "worker_execution"
            result.reason = (
                "the worker task failed before producing output and no provider error was observed"
            )
    elif result.reason == "worker output claimed verification":
        result.failure_category = result.failed_stage = "worker_contract"
    elif not asked:
        result.failure_category = "valid_response_without_tool_call"
        result.failed_stage = "model_tool_calling"
        result.reason = result.reason or _first_failure(result, completed)
        if result.name == "negative":
            result.reason = "inconclusive: the model did not execute the tests"
    elif result.tool_errors or not authenticated:
        result.failure_category = result.failed_stage = "tool_execution_failure"
        result.reason = result.reason or _first_failure(result, completed)
    else:
        result.failure_category = result.failed_stage = "evidence_or_gate_failure"
        result.reason = result.reason or (
            "the gate completed a failing project"
            if completed and result.name == "negative"
            else _first_failure(result, completed)
        )


def _describe(error: SafeError) -> str:
    parts = [error.stage, error.category]
    if error.http_status is not None:
        parts.append(f"HTTP {error.http_status}")
    if error.error_code:
        parts.append(f"code {error.error_code}")
    if error.error_param:
        parts.append(f"param {error.error_param}")
    return ": ".join(parts[:2]) + (f" ({', '.join(parts[2:])})" if parts[2:] else "")


def _first_failure(result: ScenarioReport, completed: bool) -> str:
    if result.stages.model_tool_calling != "pass":
        return "the model did not call testing.run"
    if not result.sandbox_executed:
        return "no authenticated sandbox evidence"
    if not completed:
        return f"completion gate did not pass ({result.gate_reason or result.gate_status})"
    return "unexpected result"


def strict_schema_issues(tool: Mapping[str, Any]) -> list[str]:
    """Objects in a strict function schema that are not closed with additionalProperties false.

    The OpenAI function-calling and structured-outputs guides require
    `additionalProperties: false` on every object of a strict schema.
    """

    if tool.get("strict") is not True:
        return []
    issues: list[str] = []

    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if (node.get("type") == "object" or "properties" in node) and node.get(
                "additionalProperties"
            ) is not False:
                issues.append(f"{tool.get('name')}: {path or 'parameters'} is an open object")
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    for name, child in value.items():
                        visit(child, f"{path}.{name}" if path else name)
                elif key in {"items", "anyOf", "oneOf", "allOf", "$defs", "definitions"}:
                    visit(value, path)
        elif isinstance(node, list):
            for value in node:
                visit(value, path)

    visit(tool.get("parameters"), "")
    return issues


class _NullRunner:
    """Stands in for a sandbox only to build tool schemas offline. It never runs anything."""

    provides_isolation = True

    def __init__(self, filesystem: WorkspaceFilesystem) -> None:
        self.filesystem = filesystem
        self.root = "main"

    async def run(self, command: Any) -> Any:
        raise AssertionError("the schema probe never executes a command")


def probe_openai_tool_schemas() -> list[str]:
    """Offline check of the exact strict tool schemas the OpenAI adapter would send."""

    from kaiwen_agent.models.openai_tools import serialize_openai_tool

    with tempfile.TemporaryDirectory(prefix="kaiwen-probe-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        make_project(root)
        filesystem = WorkspaceFilesystem(workspace_id="probe", roots={"main": root})
        catalog = CommandCatalog(
            filesystem=filesystem,
            root="main",
            runner=_NullRunner(filesystem),
            presets=(
                CommandPreset(
                    id="unit.tests", category="test", executable="python", arguments=("-V",)
                ),
            ),
        )
        issues: list[str] = []
        for definition in testing_tools(catalog, unattended_categories=frozenset({"test"})):
            issues.extend(strict_schema_issues(serialize_openai_tool(definition)))
        return issues


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Opt-in live validation of a real model through the Kaiwen worker stack. "
        "Nothing external runs without --live."
    )
    parser.add_argument("provider", choices=("openai", "ollama"))
    parser.add_argument(
        "--model", help="OpenAI: OPENAI_MODEL or gpt-5.4-mini. Ollama: qwen2.5-coder:7b"
    )
    parser.add_argument("--live", action="store_true", help="Actually call the model")
    parser.add_argument("--scenario", choices=("pass", "negative", "both"), default="both")
    parser.add_argument("--prompt", choices=("tester", "compact"), default="tester")
    parser.add_argument("--max-requests", type=int, default=4)
    parser.add_argument("--max-steps", type=int, default=4)
    parser.add_argument("--max-tool-calls", type=int, default=3)
    parser.add_argument("--max-output-tokens", type=int, default=1_500)
    parser.add_argument("--max-input-tokens", type=int, default=6_000)
    parser.add_argument("--request-timeout", type=float, default=90.0)
    parser.add_argument("--timeout", type=float, default=600.0, help="Overall seconds")
    parser.add_argument("--budget-usd", type=float, default=0.20)
    parser.add_argument("--price-input-per-mtok", type=float)
    parser.add_argument("--price-output-per-mtok", type=float)
    parser.add_argument("--ollama-host")
    parser.add_argument("--report", type=Path, help="Write the redacted JSON report here")
    return parser


def config_from(arguments: argparse.Namespace, environ: Mapping[str, str]) -> Config:
    openai = arguments.provider == "openai"
    model = arguments.model or (
        environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL) if openai else DEFAULT_OLLAMA_MODEL
    )
    if openai:
        pricing = Pricing(
            arguments.price_input_per_mtok or ASSUMED_OPENAI_PRICE[0],
            arguments.price_output_per_mtok or ASSUMED_OPENAI_PRICE[1],
        )
    else:
        pricing = Pricing()
    return Config(
        provider=arguments.provider,
        model=model,
        live=bool(arguments.live),
        prompt="coding.compact" if arguments.prompt == "compact" else "coding.tester",
        scenario=arguments.scenario,
        limits=Limits(
            max_requests=arguments.max_requests,
            max_steps=arguments.max_steps,
            max_tool_calls=arguments.max_tool_calls,
            max_output_tokens=arguments.max_output_tokens,
            max_input_tokens=arguments.max_input_tokens,
            request_timeout=arguments.request_timeout,
            total_timeout=arguments.timeout,
            budget_usd=arguments.budget_usd,
        ),
        pricing=pricing,
        ollama_host=arguments.ollama_host,
        image=environ.get("KAIWEN_DOCKER_IMAGE", DEFAULT_IMAGE),
    )


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    try:
        config = config_from(arguments, os.environ)
        config.limits.validate()
    except ValueError as error:
        print(f"Invalid configuration: {error}", file=sys.stderr)
        return 2
    estimate = worst_case_cost(config.limits, config.pricing)
    if config.live and config.provider == "openai" and estimate > config.limits.budget_usd:
        print(
            f"Refused: worst-case estimate ${estimate:.4f} exceeds the budget "
            f"${config.limits.budget_usd:.2f}. Lower the limits or raise --budget-usd "
            "deliberately.",
            file=sys.stderr,
        )
        return 2
    report = asyncio.run(run_validation(config))
    text = render_report(report)
    if arguments.report is not None:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    if not config.live:
        print("Dry run: no model was called. Add --live to execute.", file=sys.stderr)
        return 0
    return 0 if report.verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
