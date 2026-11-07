"""Deterministic tests of the opt-in live validation harness.

No test here calls a paid API, needs a local Ollama service, an API key or Docker. The model
client and the sandbox runner are scripted fakes, so these prove configuration, limits,
failure reporting, redaction and the host-side judgement. They do not show that any real
model can do the task; that is what the manual `--live` run is for.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.toolkits import SandboxResult, SandboxUnavailableError, SnapshotBuilder
from scripts import live_validation as lv

SECRET = "sk-test-0123456789abcdefghij"
RUN_TESTS = {"command_id": "unit.tests", "parameters": {}}


class FakeRunner:
    """Trusted deterministic stand-in for the Docker runner: real snapshot, no container."""

    provides_isolation = True

    def __init__(self, filesystem: WorkspaceFilesystem, *, available: bool = True) -> None:
        self.filesystem = filesystem
        self.root = "main"
        self.available = available
        self.builder = SnapshotBuilder(filesystem, "main")
        self.calls = 0

    async def verify(self) -> None:
        if not self.available:
            raise SandboxUnavailableError("no engine")

    async def run(self, command: Any) -> SandboxResult:
        self.calls += 1
        snapshot = self.builder.capture()
        try:
            namespace: dict[str, Any] = {}
            exec((snapshot.directory / "pkg" / "calc.py").read_text(), namespace)  # noqa: S102
            passed = namespace["add"](2, 3) == 5
            return SandboxResult(
                exit_code=0 if passed else 1,
                stdout="ok" if passed else "FAILED",
                snapshot_fingerprint=snapshot.fingerprint,
                image_ref="fake",
                image_id="sha256:" + "a" * 64,
                snapshot_unchanged=True,
                cleanup_failed=False,
            )
        finally:
            snapshot.close()


def runner_factory(**options: Any) -> Any:
    runners: list[FakeRunner] = []

    def build(filesystem: WorkspaceFilesystem) -> FakeRunner:
        runners.append(FakeRunner(filesystem, **options))
        return runners[-1]

    build.runners = runners  # type: ignore[attr-defined]
    return build


def call_response(*, calls: int = 1, tokens: tuple[int, int] = (500, 40)) -> Any:
    return SimpleNamespace(
        id="resp",
        model="fake",
        output_text="",
        output=[
            SimpleNamespace(
                type="function_call",
                call_id=f"call_{index}",
                name="testing__run",
                arguments=json.dumps(RUN_TESTS),
            )
            for index in range(calls)
        ],
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


def text_response(text: str, tokens: tuple[int, int] = (600, 30)) -> Any:
    return SimpleNamespace(
        id="resp",
        model="fake",
        output_text=text,
        output=[],
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


class FakeOpenAI:
    def __init__(self, responses: list[Any], *, model_exists: bool = True) -> None:
        self.queue = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.model_exists = model_exists
        self.responses = SimpleNamespace(create=self._create)
        self.models = SimpleNamespace(retrieve=self._retrieve)

    async def _retrieve(self, model: str) -> Any:
        if not self.model_exists:
            raise LookupError("no such model")
        return SimpleNamespace(id=model)

    async def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        return self.queue.pop(0)


def config(**changes: Any) -> lv.Config:
    values: dict[str, Any] = {
        "provider": "openai",
        "model": "fake-model",
        "live": True,
        "pricing": lv.Pricing(2.5, 20.0),
    }
    values.update(changes)
    return lv.Config(**values)


def run(cfg: lv.Config, client: Any, factory: Any | None = None, **kwargs: Any) -> lv.Report:
    return asyncio.run(
        lv.run_validation(cfg, client=client, runner_factory=factory or runner_factory(), **kwargs)
    )


def stage_values(report: lv.Report) -> list[dict[str, str]]:
    return [
        vars(item.stages) if hasattr(item.stages, "__dict__") else {} for item in report.scenarios
    ]


# --------------------------------------------------------------------------- defaults and dry run


def test_without_live_nothing_external_runs_and_the_result_is_not_a_pass() -> None:
    client = FakeOpenAI([])
    report = asyncio.run(lv.run_validation(config(live=False), client=client, environ={}))
    assert report.mode == "dry_run" and report.verdict == "NOT_RUN"
    assert report.scenarios == [] and client.requests == []


def test_the_cli_defaults_are_a_dry_run_and_need_no_key_or_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(lv, "_real_client", lambda *a, **k: pytest.fail("a client was created"))
    for provider in ("openai", "ollama"):
        assert lv.main([provider]) == 0
    output = capsys.readouterr()
    assert '"verdict": "NOT_RUN"' in output.out and "Dry run" in output.err


def test_default_limits_are_conservative_and_the_worst_case_fits_the_target_budget() -> None:
    arguments = lv.build_parser().parse_args(["openai"])
    cfg = lv.config_from(arguments, {})
    assert cfg.model == "gpt-5.4-mini" and cfg.live is False
    limits = cfg.limits
    assert (limits.max_requests, limits.max_steps, limits.max_tool_calls) == (4, 4, 3)
    assert lv.worst_case_cost(limits, cfg.pricing) < 0.20
    assert lv.config_from(arguments, {"OPENAI_MODEL": "other"}).model == "other"
    local = lv.config_from(lv.build_parser().parse_args(["ollama"]), {})
    assert local.model == "qwen2.5-coder:7b" and lv.worst_case_cost(limits, local.pricing) == 0


def test_a_live_openai_run_whose_worst_case_exceeds_the_budget_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(lv.asyncio, "run", lambda *a, **k: pytest.fail("a run started"))
    code = lv.main(["openai", "--live", "--max-requests", "40"])
    assert code == 2 and "Refused" in capsys.readouterr().err


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError):
        lv.Limits(max_requests=0).validate()
    assert lv.main(["ollama", "--max-steps", "0"]) == 2


def test_prompts_come_from_the_registered_role_prompts() -> None:
    from kaiwen_agent.kits.coding.prompting import coding_prompt_definitions

    registered = {item.id: item for item in coding_prompt_definitions()}
    for name in ("coding.tester", "coding.compact"):
        text, version, _ = lv.load_instructions(name)
        assert text == registered[name].template and version == 1
    parsed = lv.build_parser().parse_args(["ollama", "--prompt", "compact"])
    assert lv.config_from(parsed, {}).prompt == "coding.compact"


# ------------------------------------------------------------------------------- the real path


def test_a_scripted_model_passes_both_scenarios_through_the_real_worker_stack() -> None:
    client = FakeOpenAI(
        [
            call_response(),
            text_response("The tests ran and passed."),
            call_response(),
            text_response("All tests pass!"),  # a confident claim about a failing project
        ]
    )
    report = run(config(), client)
    assert report.verdict == "PASS", report.failure
    good, bad = report.scenarios
    for item in (good, bad):
        stages = item.stages
        assert (stages.provider_connection, stages.model_tool_calling) == ("pass", "pass")
        assert (stages.tool_execution, stages.evidence_authentication) == ("pass", "pass")
        assert item.sandbox_executed and item.worker_verified_field is False
        assert item.snapshot_fingerprint is not None and len(item.snapshot_fingerprint) == 16
    assert (good.sandbox_exit_code, good.stages.completion_gate) == (0, "pass")
    assert (bad.sandbox_exit_code, bad.stages.completion_gate) == (1, "fail")
    assert bad.model_claimed_success is True and bad.stages.overall == "pass"
    assert good.worker_verified_field is False  # even when the gate passes
    assert report.model_requests == 4
    assert (report.input_tokens, report.output_tokens) == (500 + 600 + 500 + 600, 140)
    assert report.estimated_cost_usd == pytest.approx((2200 * 2.5 + 140 * 20.0) / 1e6, abs=1e-6)


def test_every_request_is_sent_unstored_with_an_output_cap_and_no_secrets() -> None:
    client = FakeOpenAI([call_response(), text_response("done")])
    cfg = config(scenario="pass", limits=lv.Limits(max_output_tokens=777))
    report = run(cfg, client, environ={"OPENAI_API_KEY": SECRET})
    assert report.verdict == "PASS"
    assert client.requests and all(
        item["store"] is False and item["max_output_tokens"] == 777 for item in client.requests
    )
    assert SECRET not in json.dumps(client.requests, default=str)
    assert "Objective" in json.dumps(client.requests[0], default=str)


def test_a_model_that_only_claims_success_is_not_a_pass() -> None:
    client = FakeOpenAI([text_response("All tests pass, trust me.")])
    report = run(config(scenario="pass"), client)
    scenario = report.scenarios[0]
    assert report.verdict == "FAIL"
    assert scenario.stages.model_tool_calling == "fail"
    assert scenario.stages.evidence_authentication == "not_run" and not scenario.sandbox_executed
    assert scenario.stages.completion_gate == "fail" and scenario.stages.overall == "fail"
    assert scenario.failure_category == "valid_response_without_tool_call"
    assert scenario.worker_output == "available" and scenario.worker_verified_field is False
    assert scenario.model_claimed_success is True
    assert "did not call testing.run" in (scenario.reason or "")


def test_a_negative_run_where_the_model_never_ran_the_tests_is_inconclusive() -> None:
    client = FakeOpenAI([text_response("It passes.")])
    report = run(config(scenario="negative"), client)
    scenario = report.scenarios[0]
    assert scenario.stages.overall == "fail" and "inconclusive" in (scenario.reason or "")
    assert report.verdict == "FAIL"


def test_the_request_limit_aborts_the_run() -> None:
    client = FakeOpenAI([call_response(), text_response("done")])
    report = run(config(scenario="pass", limits=lv.Limits(max_requests=1)), client)
    assert report.verdict == "FAIL" and report.abort_reason == "request limit reached"
    assert len(client.requests) == 1 and report.model_requests == 1


def test_the_tool_call_limit_aborts_before_the_tools_run() -> None:
    client = FakeOpenAI([call_response(calls=2)])
    factory = runner_factory()
    report = run(config(scenario="pass", limits=lv.Limits(max_tool_calls=1)), client, factory)
    assert report.abort_reason == "tool-call limit exceeded" and report.verdict == "FAIL"
    assert all(item.calls == 0 for item in factory.runners)


def test_the_budget_estimate_stops_a_request_before_it_is_sent() -> None:
    client = FakeOpenAI([call_response()])
    report = run(config(scenario="pass", limits=lv.Limits(budget_usd=0.0001)), client)
    assert report.abort_reason == "budget estimate exceeded" and client.requests == []
    assert report.estimated_cost_usd == 0.0 and report.verdict == "FAIL"


def test_the_input_size_limit_stops_a_request_before_it_is_sent() -> None:
    client = FakeOpenAI([call_response()])
    report = run(config(scenario="pass", limits=lv.Limits(max_input_tokens=10)), client)
    assert report.abort_reason == "input token limit exceeded" and client.requests == []


def test_a_missing_model_or_key_is_reported_without_a_model_request() -> None:
    absent = FakeOpenAI([], model_exists=False)
    report = run(config(), absent)
    assert report.verdict == "FAIL" and "provider unavailable" in (report.failure or "")
    assert absent.requests == [] and report.scenarios == []
    keyless = asyncio.run(lv.run_validation(config(), environ={}))
    assert keyless.verdict == "FAIL" and "not configured" in (keyless.failure or "")


def test_an_unavailable_sandbox_is_not_a_pass_and_makes_no_model_request() -> None:
    client = FakeOpenAI([call_response()])
    report = run(config(scenario="pass"), client, runner_factory(available=False))
    assert report.docker == "unavailable" and report.verdict == "FAIL"
    assert client.requests == [] and report.scenarios[0].stages.overall == "not_run"


def test_the_overall_timeout_is_reported() -> None:
    class Slow(FakeOpenAI):
        async def _create(self, **request: Any) -> Any:
            await asyncio.sleep(5)

    report = run(config(scenario="pass", limits=lv.Limits(total_timeout=0.2)), Slow([]))
    assert report.verdict == "FAIL" and report.failure == "overall timeout reached"


def test_secrets_are_scrubbed_from_the_rendered_report() -> None:
    report = lv.Report(provider="openai", model="m", prompt="p", mode="live")
    report.failure = f"upstream said key {SECRET} and Bearer abcdefghijklmnop"
    text = lv.render_report(report, {"OPENAI_API_KEY": SECRET, "OTHER": "visible-value"})
    assert SECRET not in text and "abcdefghijklmnop" not in text and "[REDACTED]" in text
    assert str(Path.home()) not in text


def test_the_report_has_no_prompt_text_and_no_filesystem_paths() -> None:
    client = FakeOpenAI([call_response(), text_response("done")])
    report = run(config(scenario="pass"), client)
    text = lv.render_report(report, {})
    assert "kaiwen-live-" not in text and "ROLE-SPECIFIC" not in text
    assert "\\\\" not in text and "/tmp" not in text


# ----------------------------------------------------------------------------------- Ollama


class FakeOllama:
    def __init__(self, responses: list[dict[str, Any]], *, installed: tuple[str, ...]) -> None:
        self.queue = list(responses)
        self.installed = installed
        self.requests: list[dict[str, Any]] = []

    async def list(self) -> dict[str, Any]:
        return {"models": [{"model": name} for name in self.installed]}

    async def ps(self) -> dict[str, Any]:
        return {"models": [{"model": "qwen2.5-coder:7b", "size": 100, "size_vram": 40}]}

    async def chat(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return self.queue.pop(0)


def ollama_message(content: str = "", *, call: bool = False) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if call:
        message["tool_calls"] = [{"function": {"name": "testing__run", "arguments": RUN_TESTS}}]
    return {
        "model": "qwen2.5-coder:7b",
        "message": message,
        "prompt_eval_count": 300,
        "eval_count": 20,
        "eval_duration": 1_000_000_000,
    }


def ollama_config(**changes: Any) -> lv.Config:
    values: dict[str, Any] = {
        "provider": "ollama",
        "model": "qwen2.5-coder:7b",
        "pricing": lv.Pricing(),
    }
    values.update(changes)
    return config(**values)


def test_an_ollama_model_runs_with_bounded_options_and_reports_what_it_can() -> None:
    client = FakeOllama(
        [
            ollama_message(call=True),
            ollama_message("ran"),
            ollama_message(call=True),
            ollama_message("ok"),
        ],
        installed=("qwen2.5-coder:7b",),
    )
    report = run(ollama_config(), client)
    assert report.verdict == "PASS", report.failure
    assert report.estimated_cost_usd == 0.0 and report.model_requests == 4
    assert all(
        item["options"]["num_predict"] == 1_500 and item["options"]["temperature"] == 0
        for item in client.requests
    )
    assert report.hardware["gpu_offload_percent"] == 40
    assert report.hardware["generation_tokens_per_second"] == 20.0


def test_an_uninstalled_ollama_model_is_never_pulled_or_run() -> None:
    client = FakeOllama([], installed=("llama3.2:latest",))
    report = run(ollama_config(), client)
    assert report.verdict == "FAIL" and "provider unavailable" in (report.failure or "")
    assert client.requests == []


def test_the_qwen3_family_disables_thinking_and_others_do_not_get_the_flag() -> None:
    qwen3 = lv.build_provider(ollama_config(model="qwen3:4b"), object(), "x")
    coder = lv.build_provider(ollama_config(), object(), "x")
    assert qwen3.think is False and coder.think is None  # type: ignore[union-attr]
