"""Offline tests of the live-validation failure diagnostics.

Real OpenAI SDK exception types are constructed locally and raised by fake clients, so no test
needs credentials, network access or a paid call. They prove the harness records only an
allowlist of structured facts about a failure, never the exception text, and that it does not
misreport an unknown outcome (failed task, missing usage) as a worker claim or as zero cost.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from pydantic import BaseModel, ValidationError

from scripts import live_validation as lv
from tests.test_live_validation import (
    SECRET,
    FakeOllama,
    FakeOpenAI,
    call_response,
    config,
    ollama_config,
    run,
    runner_factory,
    text_response,
)

REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")
LEAK = f"Incorrect API key provided: {SECRET}; body={{'input': 'PRIVATE PROMPT TEXT'}}"


def status_error(cls: type[openai.APIStatusError], status: int, **body: Any) -> Exception:
    response = httpx.Response(status, request=REQUEST, headers={"authorization": SECRET})
    return cls(LEAK, response=response, body=body or None)


class Failing(FakeOpenAI):
    def __init__(self, error: Exception, **options: Any) -> None:
        super().__init__([], **options)
        self.error = error

    async def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        raise self.error


def failed(error: Exception) -> tuple[lv.Report, lv.ScenarioReport]:
    report = run(config(scenario="pass"), Failing(error), environ={"OPENAI_API_KEY": SECRET})
    return report, report.scenarios[0]


def assert_clean(report: lv.Report) -> None:
    text = lv.render_report(report, {"OPENAI_API_KEY": SECRET})
    for forbidden in (SECRET, "PRIVATE PROMPT TEXT", "Incorrect API key", "authorization"):
        assert forbidden not in text, forbidden


def assert_unknown_outcome(report: lv.Report, scenario: lv.ScenarioReport) -> None:
    assert report.verdict == "FAIL"
    assert scenario.worker_output == "unavailable" and scenario.worker_verified_field is None
    assert scenario.reason is not None and "claimed verification" not in scenario.reason
    assert scenario.task_status == "failed" and scenario.task_error_code == "execution_error"
    # Later stages never ran; they are not reported as failures of those stages.
    stages = scenario.stages
    assert (stages.model_tool_calling, stages.tool_execution) == ("not_run", "not_run")
    assert (stages.evidence_authentication, stages.completion_gate) == ("not_run", "not_run")
    assert scenario.gate_reason == "task_execution_failed"
    # An attempt without reported usage has unknown billing, never zero.
    assert (report.model_requests, report.successful_responses) == (1, 0)
    assert report.input_tokens is None and report.output_tokens is None
    assert report.estimated_cost_usd is None and "unknown" in (report.billing_note or "")
    assert report.unknown_usage_requests == 1


@pytest.mark.parametrize(
    ("cls", "status", "body", "category", "code", "param"),
    [
        (
            openai.BadRequestError,
            400,
            {
                "code": "invalid_function_parameters",
                "param": "tools[0].parameters",
                "type": "invalid_request_error",
            },
            "invalid_request",
            "invalid_function_parameters",
            "tools",
        ),
        (
            openai.AuthenticationError,
            401,
            {"code": "invalid_api_key"},
            "authentication",
            "invalid_api_key",
            None,
        ),
        (
            openai.PermissionDeniedError,
            403,
            {"code": "model_not_found"},
            "permission_denied",
            "model_not_found",
            None,
        ),
        (
            openai.RateLimitError,
            429,
            {"code": "insufficient_quota"},
            "quota_exhausted",
            "insufficient_quota",
            None,
        ),
        (
            openai.RateLimitError,
            429,
            {"code": "rate_limit_exceeded"},
            "rate_limited",
            "rate_limit_exceeded",
            None,
        ),
        (openai.InternalServerError, 503, {}, "server_error", None, None),
    ],
)
def test_http_errors_are_classified_from_structured_fields(
    cls: type[openai.APIStatusError],
    status: int,
    body: dict[str, str],
    category: str,
    code: str | None,
    param: str | None,
) -> None:
    del param  # a dotted parameter path is not an identifier; see the next test
    report, scenario = failed(status_error(cls, status, **body))
    assert_unknown_outcome(report, scenario)
    error = report.provider_error
    assert error is not None
    assert (error["category"], error["stage"], error["http_status"]) == (
        category,
        "http_error",
        status,
    )
    assert error["error_code"] == code and error["http_response_observed"] is True
    assert error["model_response_received"] is False and error["usage_available"] is False
    assert (error["attempt"], error["exception_class"]) == (1, cls.__name__)
    assert scenario.failure_category == "http_error" and scenario.failed_stage == "http_error"
    assert f"HTTP {status}" in (scenario.reason or "")
    assert_clean(report)


def test_a_safe_parameter_name_is_kept_and_an_unsafe_one_is_dropped() -> None:
    safe = status_error(openai.BadRequestError, 400, code="x", param="max_output_tokens")
    assert lv.sanitize_error(safe, attempt=1).error_param == "max_output_tokens"
    unsafe = status_error(openai.BadRequestError, 400, code=SECRET, param=f"{SECRET} body")
    sanitized = lv.sanitize_error(unsafe, attempt=1)
    assert sanitized.error_code is None and sanitized.error_param is None
    assert SECRET not in json.dumps(lv.asdict(sanitized))  # type: ignore[attr-defined]


def test_connection_and_timeout_failures_have_no_http_response() -> None:
    for error, category in (
        (openai.APIConnectionError(message=LEAK, request=REQUEST), "connection"),
        (openai.APITimeoutError(request=REQUEST), "timeout"),
    ):
        report, scenario = failed(error)
        assert_unknown_outcome(report, scenario)
        assert report.provider_error is not None
        assert report.provider_error["category"] == category
        assert report.provider_error["stage"] == "transport_failure"
        assert report.provider_error["http_response_observed"] is False
        assert report.provider_error["http_status"] is None
        assert scenario.failure_category == "transport_failure"
        assert_clean(report)


def test_sdk_side_validation_errors_are_rejections_before_transport() -> None:
    class Shape(BaseModel):
        value: int

    try:
        Shape(value="not a number")  # type: ignore[arg-type]
    except ValidationError as pydantic_error:
        errors: list[Exception] = [pydantic_error, TypeError(LEAK), ValueError(LEAK)]
    for error in errors:
        report, scenario = failed(error)
        assert_unknown_outcome(report, scenario)
        assert report.provider_error is not None
        assert report.provider_error["category"] == "sdk_validation"
        assert report.provider_error["stage"] == "rejected_before_transport"
        assert report.provider_error["http_response_observed"] is False
        assert scenario.failure_category == "rejected_before_transport"
        assert_clean(report)


def test_an_unclassifiable_failure_is_reported_as_undetermined_not_guessed() -> None:
    report, scenario = failed(RuntimeError(LEAK))
    assert report.provider_error is not None
    assert (report.provider_error["category"], report.provider_error["stage"]) == (
        "unclassified",
        "undetermined",
    )
    assert scenario.failure_category == "undetermined"
    assert_clean(report)


def test_a_failure_before_any_request_is_a_rejection_not_a_billed_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken:
        async def respond(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(LEAK)

    monkeypatch.setattr(lv, "build_provider", lambda *a, **k: Broken())
    client = FakeOpenAI([])
    report = run(config(scenario="pass"), client, environ={"OPENAI_API_KEY": SECRET})
    scenario = report.scenarios[0]
    assert client.requests == [] and report.model_requests == 0
    assert report.estimated_cost_usd == 0.0 and report.billing_note == "no request was sent"
    assert scenario.failure_category == "rejected_before_transport"
    assert report.provider_error is not None
    assert report.provider_error["category"] == "adapter_error"
    assert scenario.worker_output == "unavailable" and scenario.worker_verified_field is None
    assert_clean(report)


def test_a_failure_after_a_successful_response_is_not_blamed_on_the_transport() -> None:
    bad_arguments = call_response()
    bad_arguments.output[0].arguments = "{not json"
    report = run(
        config(scenario="pass"), FakeOpenAI([bad_arguments]), environ={"OPENAI_API_KEY": SECRET}
    )
    scenario = report.scenarios[0]
    assert report.provider_error is not None
    assert report.provider_error["stage"] == "adapter_failure_after_response"
    assert report.provider_error["model_response_received"] is True
    assert report.provider_error["http_response_observed"] is False
    assert (report.model_requests, report.successful_responses) == (1, 1)
    # The response carried usage, so the cost is known even though the task failed.
    assert report.responses_with_usage == 1 and report.input_tokens == 500
    assert report.estimated_cost_usd is not None and report.estimated_cost_usd > 0
    assert scenario.failure_category == "adapter_failure_after_response"
    assert scenario.worker_output == "unavailable"
    assert_clean(report)


def test_a_response_without_usage_leaves_cost_unknown_instead_of_zero() -> None:
    no_usage = text_response("nothing to run")
    no_usage.usage = None
    report = run(config(scenario="pass"), FakeOpenAI([no_usage]))
    scenario = report.scenarios[0]
    assert (report.model_requests, report.successful_responses) == (1, 1)
    assert report.responses_with_usage == 0 and report.unknown_usage_requests == 1
    assert report.input_tokens is None and report.estimated_cost_usd is None
    assert "unknown" in (report.billing_note or "")
    assert scenario.failure_category == "valid_response_without_tool_call"
    assert scenario.worker_output == "available" and scenario.worker_verified_field is False


def test_the_budget_counts_an_unknown_usage_attempt_at_its_worst_case() -> None:
    ledger = lv.Ledger(lv.Limits(budget_usd=0.05), lv.Pricing(2.5, 20.0))
    ledger.before(1_000)
    ledger.record_failure(openai.APITimeoutError(request=REQUEST))
    assert ledger.reported_usd == 0.0 and ledger.spent_usd > 0.0
    with pytest.raises(lv.LimitExceeded):
        ledger.before(1_000)  # a second attempt no longer fits the budget


def test_a_valid_response_without_a_tool_call_is_its_own_category() -> None:
    report = run(config(scenario="pass"), FakeOpenAI([text_response("All tests pass!")]))
    scenario = report.scenarios[0]
    assert scenario.failure_category == "valid_response_without_tool_call"
    assert scenario.failed_stage == "model_tool_calling" and scenario.model_claimed_success
    assert report.provider_error is None and report.responses_with_usage == 1
    assert scenario.worker_verified_field is False and scenario.worker_output == "available"


def test_tool_and_gate_failures_are_distinguished_from_model_failures() -> None:
    client = FakeOpenAI([call_response(), text_response("It failed.")])
    report = run(config(scenario="pass"), client, runner_factory(), environ={})
    assert report.verdict == "PASS"  # control: the working project passes
    broken = FakeOpenAI([call_response(), text_response("It failed.")])
    # The failing project in the pass scenario: the tests ran, the gate must not complete.
    from scripts import live_validation as module

    original = module.WORKING
    module.WORKING = module.BROKEN
    try:
        failing = run(config(scenario="pass"), broken)
    finally:
        module.WORKING = original
    scenario = failing.scenarios[0]
    assert scenario.stages.tool_execution == "pass" and scenario.sandbox_exit_code == 1
    assert scenario.failure_category == "evidence_or_gate_failure"
    assert scenario.failed_stage == "evidence_or_gate_failure"


def test_classification_with_no_provider_error_is_undetermined() -> None:
    result = lv.ScenarioReport(name="pass", expectation="x")
    ledger = lv.Ledger(lv.Limits(), lv.Pricing())
    lv._classify(result, ledger, False, False, False, has_output=False)
    assert result.failure_category == "undetermined" and result.failed_stage == "worker_execution"
    assert result.reason is not None and "claimed verification" not in result.reason


def test_a_missing_model_is_a_sanitized_preflight_error_with_no_request() -> None:
    class Missing(FakeOpenAI):
        async def _retrieve(self, model: str) -> Any:
            raise status_error(openai.NotFoundError, 404, code="model_not_found")

    client = Missing([])
    report = run(config(), client, environ={"OPENAI_API_KEY": SECRET})
    assert client.requests == [] and report.model_requests == 0
    assert report.failure == "provider unavailable: not_found"
    assert report.provider_error is not None
    assert (report.provider_error["http_status"], report.provider_error["error_code"]) == (
        404,
        "model_not_found",
    )
    assert report.estimated_cost_usd == 0.0
    assert_clean(report)


def test_unexpected_harness_exceptions_are_reported_by_class_name_only() -> None:
    report = asyncio.run(
        lv.run_validation(
            config(),
            client=SimpleNamespace(models=SimpleNamespace(retrieve=_boom)),
            environ={"OPENAI_API_KEY": SECRET},
        )
    )
    assert report.failure == "provider unavailable: unclassified"
    assert_clean(report)


async def _boom(model: str) -> Any:
    raise RuntimeError(LEAK)


def test_ollama_http_errors_use_the_same_allowlist() -> None:
    response = httpx.Response(500, request=httpx.Request("POST", "http://127.0.0.1:11434"))

    class Down(FakeOllama):
        async def chat(self, **request: Any) -> Any:
            raise httpx.HTTPStatusError(LEAK, request=response.request, response=response)

    report = run(ollama_config(scenario="pass"), Down([], installed=("qwen2.5-coder:7b",)))
    assert report.provider_error is not None
    assert report.provider_error["category"] == "server_error"
    assert report.provider_error["http_status"] == 500
    assert report.estimated_cost_usd == 0.0 and "local" in (report.billing_note or "")
    assert_clean(report)


def test_usage_and_cost_are_provider_reported_and_flagged_when_partial() -> None:
    no_usage = text_response("x")
    no_usage.usage = None
    ledger = lv.Ledger(lv.Limits(max_requests=4), lv.Pricing(2.5, 20.0))
    for usage in ((100, 10), None):
        ledger.before(100)
        ledger.after(*(usage or (None, None)), 0, 0.0)
    report = lv.Report(provider="openai", model="m", prompt="p", mode="live")
    lv._account(report, ledger, config())
    assert (report.model_requests, report.successful_responses) == (2, 2)
    assert (report.responses_with_usage, report.unknown_usage_requests) == (1, 1)
    assert report.cost_is_lower_bound is True and report.input_tokens == 100


# --------------------------------------------------------------- request compatibility


def test_strict_schema_checker_flags_open_objects_and_ignores_non_strict_tools() -> None:
    closed = {
        "strict": True,
        "name": "ok",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"type": "string"}},
        },
    }
    assert lv.strict_schema_issues(closed) == []
    nested = json.loads(json.dumps(closed))
    nested["parameters"]["properties"]["bag"] = {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }
    assert lv.strict_schema_issues(nested) == ["ok: bag is an open object"]
    nested["strict"] = False
    assert lv.strict_schema_issues(nested) == []


def test_openai_dry_run_reports_schema_warnings_without_a_request() -> None:
    report = asyncio.run(lv.run_validation(config(live=False), environ={}))
    assert report.verdict == "NOT_RUN" and report.model_requests == 0
    assert report.request_compatibility_warnings == lv.probe_openai_tool_schemas()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known, documented incompatibility (not yet fixed, needs an adapter decision): the "
        "OpenAI guides require additionalProperties: false on every object of a strict function "
        "schema, but testing.run is sent with strict=true and a free-form `parameters` object. "
        "Whether the API rejects it is unverified; no network request was made."
    ),
)
def test_testing_run_strict_schema_closes_every_object() -> None:
    assert lv.probe_openai_tool_schemas() == []
