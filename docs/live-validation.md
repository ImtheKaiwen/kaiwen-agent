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
`overall`. `not_run` is never a pass, and a dry run reports `NOT_RUN`. Prompts, paths and
secrets are not written; configured secret values and key-shaped strings are scrubbed.

## Troubleshooting

- `provider unavailable`: the key or service is missing, or the model is not installed or not
  available to the account. This is not a billed model request.
- `docker: unavailable`: start the engine and load the pinned image.
- `aborted: ...`: a limit was reached; raise it deliberately and rerun by hand.
- `the model did not call testing.run`: the model answered without using the tool. This is a
  model result, not a harness fault. Try the other prompt (`--prompt compact`) or model.

CI runs only the deterministic tests (`tests/test_live_validation.py`) with scripted fakes. It
makes no paid call, needs no Ollama and no key.
