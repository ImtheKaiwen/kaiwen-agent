# Live provider validation

`scripts/live_validation.py` answers one question: can a real model use Kaiwen Agent's tools to
perform a bounded coding task while the host independently verifies the result? It is a
manual validation harness, not a framework. It reuses `AgentRuntimeWorker`, `TaskScheduler`,
`WorkflowController`, the Docker sandbox, the signed evidence pipeline and the completion gate,
and the registered `coding.tester` (or `coding.compact`) role prompt, unmodified.

## What it does

For each scenario it creates a temporary Python project (an `add` function and one unittest),
builds one authorized workspace, and gives a Tester worker only the `testing.run` tool. The
model is asked to run the `unit.tests` preset once. The preset executes in the Docker sandbox
(no network, read-only root, private snapshot), the recorder writes signed evidence, and the
completion gate decides.

- `pass`: the project works. Expected: tool call, sandbox exit code 0, evidence authenticates,
  gate passes. The worker output still says `verified: false`.
- `negative`: the source is broken before the run. Expected: the tests fail in the sandbox, and
  the gate does not complete, whatever the model says. If the model never ran the tests the
  scenario is reported as inconclusive, not as a pass.

There is no code-editing loop. A natural-language "tests passed" is never evidence.

## Setup

- Docker with a Linux engine, and the digest-pinned image present locally (the same image the
  Docker examples use; `KAIWEN_DOCKER_IMAGE` overrides it). Without an engine the run reports
  `docker: unavailable` and `FAIL`, and makes no model request.
- OpenAI: `pip install -e ".[dev]"`, `OPENAI_API_KEY` in the environment or the ignored `.env`.
  Optional `OPENAI_MODEL` (default `gpt-5.4-mini`).
- Ollama: `pip install -e ".[ollama]"`, a running local service, and the model already
  installed (`qwen2.5-coder:7b`, optionally `qwen3:4b`). The harness never pulls models.

## Commands

Without `--live` the command only prints the configuration and the worst-case estimate.

```bash
python scripts/live_validation.py openai
python scripts/live_validation.py ollama --live --report .kaiwen-live/ollama-coder.json
python scripts/live_validation.py ollama --live --model qwen3:4b --prompt compact
python scripts/live_validation.py openai --live --scenario pass --report .kaiwen-live/openai.json
```

## Limits

| Option | Default | Meaning |
| --- | --- | --- |
| `--max-requests` | 4 | Model requests for the whole run, across scenarios |
| `--max-steps` | 4 | `AgentRuntime` steps per task |
| `--max-tool-calls` | 3 | Tool calls per scenario; the run aborts before tools execute |
| `--max-output-tokens` | 1500 | Sent as `max_output_tokens` (OpenAI) or `num_predict` (Ollama) |
| `--max-input-tokens` | 6000 | Pessimistic pre-request size estimate (3 characters per token) |
| `--request-timeout` / `--timeout` | 90 / 600 s | Per request, and for the whole run |
| `--budget-usd` | 0.20 | OpenAI only |

Requests use `store=false` and SDK retries are disabled, so each request is counted. One
agent runs at a time. A failed run is never retried automatically.

Cost is a local pre-request estimate plus accounting from provider-reported usage, priced with
assumed rates (`--price-input-per-mtok`, `--price-output-per-mtok`; the defaults are
placeholders, so check the current price list). It stops this process. It cannot guarantee an
exact monetary ceiling; only a spend limit on the provider account can. The command refuses a
live OpenAI run whose worst case exceeds the budget.

## Output

A redacted JSON report, printed and optionally written with `--report`. It lists provider,
model, tool calls and errors, whether a sandbox command ran, its exit code, a shortened
snapshot fingerprint, evidence authentication, the gate verdict, token usage, estimated cost,
request count, latency, the abort or failure reason, and (Ollama) GPU offload and tokens per
second when Ollama reports them. Stages are kept separate: `provider_connection`,
`model_tool_calling`, `tool_execution`, `evidence_authentication`, `completion_gate`, and
`overall`. `not_run` is never a pass, a stage after an earlier failure is `not_run` rather than
failed, and a dry run reports `NOT_RUN`. Prompts, paths and
secrets are not written; configured secret values and key-shaped strings are scrubbed.

## Failure diagnostics

A failed run records an allowlist of structured facts and nothing else. The report never
contains an exception message, a response body, a header, a prompt, tool arguments or a stack
trace; free-form text from the provider is not redacted but never copied.

`provider_error` (and the earliest failing stage in each scenario) has: `exception_class`,
`category`, `stage`, `attempt`, `http_status`, `error_code`, `error_param`, `error_type`,
`http_response_observed`, `model_response_received` and `usage_available`. Codes, parameters and
types are kept only when they are short identifier-like strings that do not look like a key.

`failure_category` is the earliest stage the host's own records confirm:

| Category | Meaning |
| --- | --- |
| `rejected_before_transport` | Raised before any request left (SDK or adapter validation) |
| `transport_failure` | Connection or timeout; no HTTP response was observed |
| `http_error` | The provider answered with an error status (400, 401/403, 429, 5xx, ...) |
| `adapter_failure_after_response` | A successful response, then an adapter or parsing failure |
| `valid_response_without_tool_call` | A good response that did not call `testing.run` |
| `tool_execution_failure` | The tool was called but no authentic evidence resulted |
| `evidence_or_gate_failure` | Evidence exists but the gate did not accept it |
| `limit_exceeded` | A configured limit stopped the run |
| `undetermined` | The task failed and the host cannot attribute it; nothing is guessed |

If the worker task fails before it produces output, `worker_output` is `unavailable` and
`worker_verified_field` is `null`; it is never reported as a claim of verification.

Usage and cost are separate. `model_requests` counts attempts that reached the transport (SDK
retries are off), `successful_responses` those that returned, and `responses_with_usage` those
that carried provider-reported usage. Tokens and `estimated_cost_usd` cover reported usage only
and are `null` when no response reported any: an attempt without usage has unknown billing,
never zero (`billing_note`, `cost_is_lower_bound`). The local budget treats such an attempt at
its worst-case estimate.

## Request compatibility

OpenAI strict function schemas require `additionalProperties: false` on every object. The
shared serializer (`serialize_openai_tool`) therefore chooses strict mode per tool: it stays on
when every object can be closed, and a tool with a free-form mapping field is sent with
`strict: false` and its original JSON Schema. The Coding Kit command tools (`testing.run`,
`quality.lint`, `quality.typecheck`, `build.run`) take `parameters: dict[str, str]`, so they are
non-strict. This changes only what the provider is told; the host still validates every
argument (Pydantic, the registered preset allowlist, the exact parameter set, workspace
authorization, permissions, the sandbox, signed evidence and the gate).

The harness computes the exact tool definitions offline and reports `openai_tool_strictness`
and `request_compatibility_warnings` (strict tools whose schema still has an open object; it
should be empty). Whether a given model accepts every other keyword in a strict schema, such as
string length limits, is not verified here.

## Observed live results (manual, not CI-attested)

These two runs were made by hand on one developer machine (Windows 11, Docker Desktop with a
Linux engine) with `gpt-5.4-mini`, the `coding.tester` prompt, and the limits described above
(2 requests, 3 steps, 2 tool calls, $0.03 local budget). CI does not run them and cannot
reproduce them; the raw local reports are not stored in the repository. Costs are estimates
from provider-reported usage at the published rates of $0.75 per million input tokens and
$4.50 per million output tokens, not billing records.

| | `pass` scenario | `negative` scenario |
| --- | --- | --- |
| Project | working | deliberately broken |
| Harness verdict | PASS | PASS |
| Model requests | 2 (both with usage) | 2 (both with usage) |
| Structured `testing.run` calls | 1, no tool error | 1, no tool error |
| Docker sandbox command ran | yes | yes |
| Sandbox exit code | 0 | 1 |
| Evidence signature authenticated | yes | yes |
| Completion gate | completed | blocked (`quality_blocked`) |
| Worker `verified` field | false | false |
| Tokens (input / output) | 2581 / 73 | 2669 / 87 |
| Estimated cost | $0.002264 | $0.002393 |
| Elapsed | 19.57 s | 11.49 s |

The negative scenario passes because the host rejected failing tests, not because the workflow
completed. In neither run did the model claim success without a tool call, so the "confident
false claim" case is covered only by the deterministic tests.

What this does and does not show:

- A real model completed the full path (tool call, container execution, signed evidence, gate)
  in both directions on one model and one tiny project. It is a smoke test, not a measure of
  reliability, model quality, or general coding-agent capability.
- A first live attempt failed before any response; it was followed by diagnostics and the
  strict-schema fix above, and the later runs passed. The original failure's cause was never
  captured, so the strict-schema explanation is likely but unproven.
- The Ollama models tried locally (`qwen2.5-coder:7b`, `qwen3:4b`) did not call the tool
  (the first wrote the call as text, the second hit the output cap while reasoning). That is
  unresolved and is not covered by these results.
- The snapshot comparison between the evidence and the gate's fingerprint (`evidence_scope`)
  was added after these runs, so they were not checked this way. The controller exposes the
  gate's fingerprint only on completion, so a blocked run reports `unchecked`.
- The harness honors the OpenAI SDK's own environment (for example a custom base URL). Do not
  run it with an untrusted environment.

## Troubleshooting

- `provider unavailable`: the key or service is missing, or the model is not installed or not
  available to the account. This is not a billed model request.
- `docker: unavailable`: start the engine and load the pinned image.
- `aborted: ...`: a limit was reached; raise it deliberately and rerun by hand.
- `the model did not call testing.run`: the model answered without using the tool. This is a
  model result, not a harness fault. Try the other prompt (`--prompt compact`) or model.

CI runs only the deterministic tests (`tests/test_live_validation.py`) with scripted fakes. It
makes no paid call, needs no Ollama and no key.
