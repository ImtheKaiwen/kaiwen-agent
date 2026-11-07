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
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    spent_usd: float = 0.0
    scenario_tool_calls: int = 0
    abort_reason: str | None = None
    request_seconds: float = 0.0

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

    def after(self, input_tokens: int, output_tokens: int, tool_calls: int, seconds: float) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.spent_usd += self.pricing.cost(input_tokens, output_tokens)
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
        response = await self._inner.responses.create(**request)
        usage = getattr(response, "usage", None)
        calls = sum(
            1
            for item in getattr(response, "output", [])
            if getattr(item, "type", None) == "function_call"
        )
        self._ledger.after(
            _count(getattr(usage, "input_tokens", 0)),
            _count(getattr(usage, "output_tokens", 0)),
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
        response = await self._inner.chat(**request)
        raw = _as_mapping(response)
        message = _as_mapping(raw.get("message", {}))
        calls = len(message.get("tool_calls") or [])
        output_tokens = _count(raw.get("eval_count"))
        self.eval_tokens += output_tokens
        self.eval_seconds += _count(raw.get("eval_duration")) / 1e9
        self._ledger.after(
            _count(raw.get("prompt_eval_count")), output_tokens, calls, time.monotonic() - started
        )
        return response


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
    worker_verified_field: bool | None = None
    model_claimed_success: bool | None = None
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
    model_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None
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
    except Exception as error:  # noqa: BLE001 - reported, never raised past the harness
        report.failure = f"{type(error).__name__}: {str(error)[:300]}"
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
        raise RuntimeError(f"provider unavailable: {type(error).__name__}") from error
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
        report.model_requests = ledger.requests
        report.input_tokens = ledger.input_tokens
        report.output_tokens = ledger.output_tokens
        report.request_seconds = round(ledger.request_seconds, 2)
        if config.provider == "openai":
            report.estimated_cost_usd = round(ledger.spent_usd, 6)
        else:
            report.estimated_cost_usd = 0.0
            report.hardware = await _ollama_hardware(inner, guarded, config.model)


def _real_client(config: Config, environment: Mapping[str, str]) -> Any:
    if config.provider == "openai":
        if not environment.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not configured")
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
        raise RuntimeError("Docker sandbox unavailable") from None
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
        model=build_provider(config, client, instructions),
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
    calls = [item for item in output.get("tool_calls", []) if isinstance(item, dict)]
    result.tool_calls = calls[:10]
    result.tool_errors = sum(1 for item in calls if item.get("is_error"))
    result.worker_verified_field = output.get("verified") if output else None
    summary = str(output.get("summary", "")).lower()
    result.model_claimed_success = bool(re.search(r"\b(pass|passed|passes|success)", summary))
    result.gate_status = outcome.status.value
    result.gate_reason = outcome.reason
    asked = any(item.get("tool") == "testing.run" for item in calls)
    result.stages.model_tool_calling = "pass" if asked else "fail"

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
    result.stages.tool_execution = "pass" if authenticated else "fail"
    result.stages.evidence_authentication = "pass" if authenticated else "fail"
    if authenticated:
        latest = authenticated[-1]
        result.sandbox_exit_code = latest.get("exit_code")
        fingerprint = latest.get("snapshot_fingerprint")
        result.snapshot_fingerprint = fingerprint[:16] if isinstance(fingerprint, str) else None
    completed = outcome.status == WorkflowOutcomeStatus.COMPLETED
    result.stages.completion_gate = "pass" if completed else "fail"
    if result.worker_verified_field is not False:
        result.reason = "worker output claimed verification"
        result.stages.overall = "fail"
    elif result.name == "pass":
        ok = asked and bool(authenticated) and completed and result.sandbox_exit_code == 0
        result.stages.overall = "pass" if ok else "fail"
        if not ok:
            result.reason = ledger.abort_reason or _first_failure(result, completed)
    else:
        # The model must have run the tests; only then does a block prove anything.
        failed_run = bool(authenticated) and result.sandbox_exit_code not in (0, None)
        ok = asked and failed_run and not completed
        result.stages.overall = "pass" if ok else "fail"
        if not ok:
            result.reason = ledger.abort_reason or (
                "the gate completed a failing project"
                if completed
                else "inconclusive: the model did not execute the tests"
            )


def _first_failure(result: ScenarioReport, completed: bool) -> str:
    if result.stages.model_tool_calling != "pass":
        return "the model did not call testing.run"
    if not result.sandbox_executed:
        return "no authenticated sandbox evidence"
    if not completed:
        return f"completion gate did not pass ({result.gate_reason or result.gate_status})"
    return "unexpected result"


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
