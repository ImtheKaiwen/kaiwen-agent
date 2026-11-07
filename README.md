# Kaiwen Agent Platform

Provider-neutral, reusable agent runtime and UI protocol. This repository contains
the reusable platform only; product-specific prompts, tools, persistence models,
routes, and visual themes belong in consuming applications.

The project is an alpha. Pin exact versions in consuming applications.

## Packages

| Package | Registry | Location | Purpose |
| --- | --- | --- | --- |
| `kaiwen-agent` | PyPI | repository root (`src/`) | Trusted server runtime (Python 3.11+) |
| `@imthekaiwen/agent-protocol` | npm | `packages/protocol/` | Versioned JSON Schema and TypeScript contracts |
| `@imthekaiwen/agent-ui` | npm | `packages/ui-agent/` | Headless browser runtime and optional React bindings |

Package versions are independent. See [`CHANGELOG.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/CHANGELOG.md) for the Python
package history and [`docs/releasing.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/releasing.md) for the release procedure.

## Repository layout

```text
src/kaiwen_agent/   Python package source
tests/              Python tests
pyproject.toml      Python package metadata and tool configuration
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

## Python quickstart

```python
import asyncio

from pydantic import BaseModel

from kaiwen_agent import Agent, AgentContext, tool
from kaiwen_agent.models.openai import OpenAIResponsesProvider
from kaiwen_agent.types import AgentInput


class ProjectQuery(BaseModel):
    category: str | None = None


@tool(
    name="projects.read",
    description="List projects visible to the current user",
    input_model=ProjectQuery,
    permissions={"projects.read"},
)
async def read_projects(
    context: AgentContext,
    query: ProjectQuery,
) -> list[dict[str, str]]:
    repository = context.services["projects"]
    return await repository.list(category=query.category)


async def main() -> None:
    agent = Agent(
        name="portfolio",
        model=OpenAIResponsesProvider(
            model="your-model-id",
            instructions="Help visitors discover relevant products.",
            store=False,
        ),
        tools=[read_projects],
        permissions={"projects.read"},
    )
    result = await agent.run(
        AgentInput(text="Show me the games"),
        context=AgentContext(
            session_id="session-id",
            permissions=frozenset({"projects.read"}),
            services={"projects": your_repository},
        ),
    )
    print(result.text)


asyncio.run(main())
```

For a single-agent composition that keeps normal chat tool-free and makes action
tools explicit:

```python
from kaiwen_agent import AgentRuntime, RuntimeIdentity

runtime = AgentRuntime(name="assistant", model=provider, tools=[read_projects])
reply = await runtime.chat(
    "Merhaba",
    identity=RuntimeIdentity(session_id="session-id"),
)
```

`AgentRuntime.action()` exposes only the tool names selected by trusted host code.
`AgentRuntime.run()` keeps workflow mode fail-closed. Autonomous hosts explicitly
compose `WorkflowController` with durable run state, the existing Task Runtime, and
completion gates; it never falls back to an unsafe in-memory workflow.

Larger applications can register immutable `AgentDefinition` records and compose
them through `AgentFactory` plus a strict `WorkspaceProfile`. The factory resolves
exact prompt and capability versions, constructs a fresh provider instance, validates
tool visibility against both capability and workspace allowlists, and records the
selected versions in runtime context. Policy and execution-time permission checks
remain authoritative; configuration never grants additional access.

Applications may inject `ContextBuilder`, `ContextWindow`, and
`ContextCompactionManager` into the runtime. Context collection stays optional for
simple chat, uses a provider-specific `TokenCounter` when available, and never treats
lossy session summaries as policy or configuration. Use
`ArtifactToolResultProcessor` when large tool output should be stored out-of-band and
represented to the model by an owner-scoped artifact reference.

Runtime delivery requirements use immutable `StandardsPack` definitions and
host-registered verifiers. `CompletionGate` accepts a mandatory requirement only when
its artifact evidence is owned by the trusted user, belongs to the current run, and
matches both the protected input fingerprint and declared evidence type. The package
does not include product-specific test, legal, citation, or CI verifiers.

`OPENAI_API_KEY` is read by the OpenAI SDK from the server environment. The
runtime never stores it and it must never be exposed to browser code.

### Side effects

Tools marked `reversible` or `destructive` receive a runtime idempotency key.
Set `requires_approval=True` and supply an application-owned `ApprovalGate` for
publishing, deleting, deploying, calling, or sending messages. Applications
must still perform authorization inside their own service boundary. Memory setup and
its trust boundaries are documented in `docs/long-term-memory.md`.

## Implemented capabilities

The checked-out code is authoritative; [`docs/roadmap.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/roadmap.md) tracks
status per phase.

- Async agent loop with a central tool pipeline: schema validation, policy,
  approval, idempotency, timeout and retry, audit, and events; session, run, event,
  and audit ports with a SQLite reference adapter, and SSE event fan-out.
- `AgentRuntime`: single-agent-first facade with explicit chat and action modes,
  trusted identity, immutable definitions, optional context and compaction.
- Durable action intents, one-time approvals, and restart recovery for workflows.
- Task runtime, background jobs, bounded delegation, optional teams.
- Lifecycle wakeups, triggers, objectives, watches, and attention evaluation.
- Short-term summaries and policy-gated long-term memory (in-memory and optional
  PostgreSQL full-text and pgvector hybrid retrieval).
- Optional Redis lease coordination with fencing tokens.
- Root-bounded workspace filesystem tools and trusted MCP bindings that use the
  central tool safety pipeline.
- Technical model capability records and data-locality-aware routing that prevents
  cloud fallback for `local_only` requests.
- Model routing, budgets, prompt and skill registries, artifacts, evaluation,
  replay, dead letters, backpressure, health, and metrics ports.
- Providers: OpenAI Responses, OpenAI Live (WebSocket, WebRTC bootstrap, and trusted
  sideband), optional Gemini and Ollama whose SDKs only return tool proposals, with
  deterministic fakes for tests.
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
  [`docs/roadmap.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/roadmap.md).

## Examples

Provider-free examples run without credentials:
[`examples/basic-agent`](https://github.com/ImtheKaiwen/kaiwen-agent/tree/main/examples/basic-agent) (minimal `Agent` with a tool),
[`examples/simple-runtime`](https://github.com/ImtheKaiwen/kaiwen-agent/tree/main/examples/simple-runtime) (`AgentRuntime` chat quickstart),
and [`examples/speech-pipeline`](https://github.com/ImtheKaiwen/kaiwen-agent/tree/main/examples/speech-pipeline) (ephemeral STT/TTS
composition). Further examples cover task graphs, autonomous and team workflows,
Coding Kits, and the React UI under [`examples/`](https://github.com/ImtheKaiwen/kaiwen-agent/tree/main/examples). Give
[`AGENTS.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/AGENTS.md) and [`docs/consumer-agent-brief.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/consumer-agent-brief.md)
to a coding agent that integrates the package.

## Local development

Python commands run from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests examples
```

Node packages are built and tested from the repository root with `npm ci`,
`npm run build`, `npm test`, and `npm run typecheck`. The full verification list is in
[`AGENTS.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/AGENTS.md).

No provider credentials are required for the core test suite or the provider-free
examples. For the optional live OpenAI smoke test, copy `.env.example` to `.env`, add
the key, then run:

```powershell
.\.venv\Scripts\python.exe examples\openai-smoke\main.py
```

## Documentation

- Contributors and coding agents: [`AGENTS.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/AGENTS.md), [`CLAUDE.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/CLAUDE.md),
  and [`docs/consumer-agent-brief.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/consumer-agent-brief.md) for integrators.
- Architecture and boundaries: [`docs/architecture.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/architecture.md) and
  the records in [`docs/decisions/`](https://github.com/ImtheKaiwen/kaiwen-agent/tree/main/docs/decisions).
- Status and plan: [`docs/roadmap.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/roadmap.md);
  runtime composition: [`docs/unified-runtime.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/unified-runtime.md).
- Coding Kits: [`docs/coding-kits/QUICKSTART.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/coding-kits/QUICKSTART.md) and
  [`docs/coding-kits/SIMPLE_API.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/coding-kits/SIMPLE_API.md).
- Browser integration: [`docs/ui-agent.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/ui-agent.md) and
  [`packages/ui-agent/INTEGRATION.md`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/packages/ui-agent/INTEGRATION.md).
- Execution and state: [`persistence`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/persistence.md),
  [`tasks`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/tasks.md), [`jobs`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/jobs.md),
  [`triggers`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/triggers.md),
  [`objectives, watches, and attention`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/objectives-watches-attention.md).
- Context and memory: [`context-memory`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/context-memory.md) and
  [`long-term-memory`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/long-term-memory.md).
- Policy, routing, and operations: [`policy`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/policy.md),
  [`runtime-registries`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/runtime-registries.md) (routing, budgets, prompt and
  skill registries, artifacts), [`production-controls`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/production-controls.md),
  and [`ecosystem-integrations`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/ecosystem-integrations.md) (workspace
  filesystem tools and MCP bindings).
- Realtime: [`realtime`](https://github.com/ImtheKaiwen/kaiwen-agent/blob/main/docs/realtime.md). Other subsystem documents are listed
  under [`docs/`](https://github.com/ImtheKaiwen/kaiwen-agent/tree/main/docs).
- Validation evidence: [`docs/validation/`](https://github.com/ImtheKaiwen/kaiwen-agent/tree/main/docs/validation) (dated snapshots).
- Historical planning documents: [`docs/archive/`](https://github.com/ImtheKaiwen/kaiwen-agent/tree/main/docs/archive).

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
