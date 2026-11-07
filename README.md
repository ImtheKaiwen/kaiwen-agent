# Kaiwen Agent Platform

Provider-neutral, reusable agent runtime and UI protocol. This repository contains
the reusable platform only; product-specific prompts, tools, persistence models,
routes, and visual themes belong in consuming applications.

The project is an alpha. Pin exact versions in consuming applications.

## Packages

| Package | Registry | Location | Purpose |
| --- | --- | --- | --- |
| `kaiwen-agent` | PyPI | `backend/` | Trusted server runtime (Python 3.11+) |
| `@imthekaiwen/agent-protocol` | npm | `packages/protocol/` | Versioned JSON Schema and TypeScript contracts |
| `@imthekaiwen/agent-ui` | npm | `packages/ui-agent/` | Headless browser runtime and optional React bindings |

Package versions are independent. See [`CHANGELOG.md`](CHANGELOG.md) for the Python
package history and [`docs/releasing.md`](docs/releasing.md) for the release procedure.

## Repository layout

```text
backend/            Python package source (src/kaiwen_agent), tests, pyproject.toml
packages/protocol/  Schemas, fixtures, and TypeScript contracts
packages/ui-agent/  Browser runtime, transports, and React components
examples/           Runnable examples, several verified in CI
docs/               Architecture, subsystem documents, ADRs, roadmap
docs/archive/       Historical planning documents (not authoritative)
```

## Install

```bash
pip install "kaiwen-agent[openai]"
npm install @imthekaiwen/agent-protocol @imthekaiwen/agent-ui
```

Optional Python extras: `openai-live`, `gemini`, `ollama`, `postgres-memory`,
`redis`.

## Implemented capabilities

The checked-out code is authoritative; [`docs/roadmap.md`](docs/roadmap.md) tracks
status per phase.

- Async agent loop with a central tool pipeline: schema validation, policy,
  approval, idempotency, timeout and retry, audit, and events.
- `AgentRuntime`: single-agent-first facade with explicit chat and action modes,
  trusted identity, immutable definitions, optional context and compaction.
- Durable action intents, one-time approvals, and restart recovery for workflows.
- Task runtime, background jobs, bounded delegation, optional teams.
- Lifecycle wakeups, triggers, objectives, watches, and attention evaluation.
- Short-term summaries and policy-gated long-term memory (in-memory and optional
  PostgreSQL/pgvector).
- Model routing, budgets, prompt and skill registries, artifacts, evaluation,
  replay, dead letters, backpressure, health, and metrics ports.
- Providers: OpenAI Responses, OpenAI Live (WebSocket and WebRTC bootstrap),
  optional Gemini and Ollama, with deterministic fakes for tests.
- Speech pipeline contracts and phone-channel contracts.
- Opt-in Coding Kits and the Simple Coding API (`kaiwen_agent.kits.coding`).
- Browser runtime with a semantic action registry, HTTP/SSE transport, and React
  Dynamic Island.

## Known limitations

- No first-party isolated command runner exists. Command, test, lint, and typecheck
  tools require a host-supplied `SandboxRunner`; only an unavailable-runner stub and
  a deterministic fake ship in the package.
- SQLite and in-memory stores are local references, not a production deployment
  choice. Distributed rate admission, live Redis and load validation, and
  operational alerting remain open.
- Gemini, Ollama, PostgreSQL, Redis, real media, and telephony providers are not
  exercised by continuous integration.
- Provider failover occurs only for exceptions a host or adapter classifies as
  retryable.
- Open audit risks and planned hardening are tracked in
  [`docs/roadmap.md`](docs/roadmap.md).

## Examples

Provider-free examples run without credentials:
[`examples/basic-agent`](examples/basic-agent) (minimal `Agent` with a tool),
[`examples/simple-runtime`](examples/simple-runtime) (`AgentRuntime` chat quickstart),
and [`examples/speech-pipeline`](examples/speech-pipeline) (ephemeral STT/TTS
composition). Further examples cover task graphs, autonomous and team workflows,
Coding Kits, and the React UI under [`examples/`](examples/).

## Local development

Python commands currently run from `backend/`:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

Node packages are built and tested from the repository root with `npm ci`,
`npm run build`, `npm test`, and `npm run typecheck`. The full verification list is in
[`AGENTS.md`](AGENTS.md).

No provider credentials are required for the core test suite or the provider-free
examples. For the optional live OpenAI smoke test, copy `.env.example` to `.env`, add
the key, then run:

```powershell
.\backend\.venv\Scripts\python.exe examples\openai-smoke\main.py
```

## Documentation

- Contributors and coding agents: [`AGENTS.md`](AGENTS.md), [`CLAUDE.md`](CLAUDE.md),
  and [`docs/consumer-agent-brief.md`](docs/consumer-agent-brief.md) for integrators.
- Architecture and boundaries: [`docs/architecture.md`](docs/architecture.md) and
  the records in [`docs/decisions/`](docs/decisions/).
- Status and plan: [`docs/roadmap.md`](docs/roadmap.md);
  runtime composition: [`docs/unified-runtime.md`](docs/unified-runtime.md).
- Coding Kits: [`docs/coding-kits/QUICKSTART.md`](docs/coding-kits/QUICKSTART.md) and
  [`docs/coding-kits/SIMPLE_API.md`](docs/coding-kits/SIMPLE_API.md).
- Browser integration: [`docs/ui-agent.md`](docs/ui-agent.md) and
  [`packages/ui-agent/INTEGRATION.md`](packages/ui-agent/INTEGRATION.md).
- Execution and state: [`persistence`](docs/persistence.md),
  [`tasks`](docs/tasks.md), [`jobs`](docs/jobs.md),
  [`triggers`](docs/triggers.md),
  [`objectives, watches, and attention`](docs/objectives-watches-attention.md).
- Context and memory: [`context-memory`](docs/context-memory.md) and
  [`long-term-memory`](docs/long-term-memory.md).
- Policy, routing, and operations: [`policy`](docs/policy.md),
  [`runtime-registries`](docs/runtime-registries.md) (routing, budgets, prompt and
  skill registries, artifacts), [`production-controls`](docs/production-controls.md),
  and [`ecosystem-integrations`](docs/ecosystem-integrations.md) (workspace
  filesystem tools and MCP bindings).
- Realtime: [`realtime`](docs/realtime.md). Other subsystem documents are listed
  under [`docs/`](docs/).
- Validation evidence: [`docs/validation/`](docs/validation/) (dated snapshots).
- Historical planning documents: [`docs/archive/`](docs/archive/).

## Design constraints

- Core code is async and independent of OpenAI, FastAPI, databases, and web
  frameworks.
- API keys and provider SDK calls remain server-side.
- Applications own domain tools and authorization context.
- Every event uses a versioned envelope.
- Tool access is deny-by-default and checked by the runtime.
- Side-effecting tools use the central approval, idempotency, policy, and audit
  pipeline.
- Browser actions are semantic; executable code and DOM selectors are never
  transmitted.
