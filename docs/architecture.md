# Architecture boundary

```text
Product application
  -> product-owned agent configuration and domain tools
  -> kaiwen-agent provider-neutral runtime
  -> provider adapter

Browser application
  -> product-owned visual theme and semantic handlers
  -> @imthekaiwen/agent-ui headless runtime
  -> versioned @imthekaiwen/agent-protocol events
```

## Repository and product relationship

This repository is the reusable platform. `kaiwen.com.tr` is a separate consuming
application and the first integration target; it does not own the platform
architecture.

- Cross-product Python orchestration belongs in `kaiwen-agent`, stable shared wire
  contracts in `@imthekaiwen/agent-protocol`, and headless browser state, transports,
  and React bindings in `@imthekaiwen/agent-ui`.
- Product tools, database models, routes, authentication composition, prompts,
  localized copy, branding, page layout, and admin workflows stay in the consuming
  application. A product feature moves here only after a product-neutral interface
  exists.
- Consumers install pinned releases and integrate through public APIs. Platform
  source is never copied into a product to bypass packaging.
- The public visitor-facing assistant and the authenticated admin agent are separate
  product surfaces. This repository owns headless UI states and a themeable
  reference component; each product owns final motion, styling, and layout.
- Text and voice converge on the same durable conversation and task systems. Parallel
  chat, voice, admin, or telephony agent implementations are avoided.
- Provider-specific behavior stays in adapters. SQLite and in-memory stores are
  references for local use, not an implicit production deployment choice.
- Short-term session summaries remain distinct from durable memory. Standards
  describe evidence-backed delivery requirements and grant no permissions.
- Implemented does not authorize publishing, pushing, or deploying; those are
  explicit release actions and published package versions are immutable.

## Composition layer

The next composition layer is governed by ADRs 0005-0007. `AgentRuntime` is a
single-agent-first facade, not another model loop or scheduler. Standards and runtime
quality remain distinct from prompts, policy, skills, and offline evaluation.
Durable workflows resume from explicit safe boundaries with at-least-once semantics.
See [`unified-runtime.md`](unified-runtime.md) for the dependency-ordered rollout.

The backend is the decision authority. Browser actions are semantic, allowlisted,
validated, and report a structured result. Arbitrary JavaScript or DOM selectors are
not part of the protocol.

Durable event ownership is explicit when a trusted runtime context provides it.
User-facing event reads require tenant, user, and session scope together; a session ID
alone is only a backward-compatible internal replay key and is not authorization.

## Text provider boundary

The core calls a small `ModelProvider` protocol. Opaque provider state remains local
to an individual run and is passed back explicitly, so a provider adapter can be
shared by concurrent sessions without sharing conversation state. The OpenAI adapter
uses response IDs when storage is enabled and carries response output items when
running statelessly. OpenAI integration is optional and does not leak SDK types into
the core.

Optional text-provider failover is a bounded provider composition, not a second model
loop. The host supplies ordered routes already approved by `ModelRouter` and creates
their server-side adapters. Failover occurs only on an explicitly classified transient
error before any response or tool-result continuation has been accepted. Thereafter,
opaque continuation state is tagged and pinned to the exact route that produced it;
it is never replayed against another provider.

## Realtime provider boundary

`kaiwen_agent.realtime` owns logical live-session state, tenant/user isolation,
concurrency limits, and provider-neutral connection primitives. Provider adapters own
transport details such as WebRTC, WebSocket, ephemeral credentials, audio formats,
and provider event translation. Provider SDK objects never cross into the core.

High-frequency audio and transcript deltas use a connection-local ephemeral event
stream. Durable session lifecycle events use a separate runtime event contract. The
shared browser protocol is not expanded until the conversation bridge can expose a
small, stable set of semantic events.

Public text/SSE assistants and authenticated admin live sessions are separate product
surfaces. Realtime credentials are issued server-side and scoped to one user,
tenant, client, and logical conversation.

## Conversation bridge

Realtime sessions are temporary transports; conversations are durable application
history. The conversation bridge assembles provider transcript fragments into
tenant-scoped text messages and never persists raw audio. Text and voice inputs share
the same conversation ID even when provider sessions reconnect.

Long work crosses a narrow task-submission port. The realtime layer does not create a
scheduler or execute workers. Agent selection is allowlisted, while tenant/user
identity comes from trusted session state rather than model-generated arguments.

## Background jobs

The job layer is a durable, user-facing projection over the existing Task Runtime.
It does not introduce a second scheduler or worker protocol. Job submission returns
immediately, while the Task Scheduler continues to own retries, resource locks,
checkpoints, timeouts, and cooperative cancellation. Text and realtime surfaces must
submit through the same `JobManager` composition.

Recovery is explicit at exclusive application startup and has at-least-once
semantics. Side-effecting workers therefore remain subject to the same approval and
idempotency controls as synchronous tools.

## Trigger routing

External events pass through deterministic filtering, tenant-aware rules, replay
deduplication, debounce, and coalescing before they can schedule a lifecycle wakeup.
The router treats payloads as untrusted data, never evaluates conditions as code,
and uses the host receipt clock for scheduling. It composes with `WakeupManager`
rather than owning a scheduler or invoking a model.

Objectives preserve durable intent separately from conversation text. Watches attach
structured external conditions to objectives and resume them through lifecycle
wakeups. Attention severity is evaluated deterministically; notification delivery is
an injected host port, not an implicit platform side effect.

## Runtime registries and budgets

Model routing returns policy-approved identifiers and never constructs provider
clients. Budget accounting is evaluated against trusted tenant/user/run/job context;
crossing a limit fails closed unless policy explicitly chooses a bounded degradation,
approval, or review path. Prompt and skill definitions are immutable and versioned,
while skill compilation targets the existing Task Runtime rather than introducing a
second executor. Artifacts are owner-scoped references for large outputs, and
capability manifests are inspectable declarations rather than authorization grants.

Runtime context composition remains an optional facade concern. Injected token
counters bound source allocation and complete request capacity; structured compaction
summarizes only canonical conversation records and cannot become an authority source.
Large tool results are replaced in model context with owner-scoped artifact references
through an executor decorator, without changing the established `Agent` loop.

Standards are immutable requirement declarations, not authorization or workflow code.
Runtime quality gates use host-registered verifiers and owner/run/input-scoped artifact
evidence. Missing verifiers, stale evidence, and unresolved mandatory conditions are
inconclusive and block completion. Offline evaluation remains a separate subsystem.

## Evaluation and production controls

Evaluation targets are injected and suites remain declarative. Replay consumes
canonical events as observations and never executes tools; simulated agent runs add a
deny-precedence side-effect policy. Dead-letter records are bounded, redacted, and
owner-scoped. Backpressure admission and health monitoring are separate ports so
distributed coordination and deployment probes can replace local references without
changing the task or agent runtimes. Credentials enter provider adapters through a
server-side `SecretProvider` boundary and are never runtime events or memory.

Metrics use a fixed catalog and trusted identity fields rather than arbitrary labels.
The core emits through an injected sink; aggregation, export, retention, dashboards,
and alerts remain deployment responsibilities.

Exclusive production ownership uses the provider-neutral `LeaseStore`. Its Redis
adapter issues fencing tokens and hashes resource identifiers before storage; hosts
must validate fencing tokens at the protected durable write boundary. Job terminal
results are delivered through an injected notifier that schedules a lifecycle wake,
while exhausted failures may be recorded in the shared dead-letter abstraction.

## Tool safety order

```text
Input validation
  -> central policy evaluation (including permission intersection)
  -> policy modification revalidation
  -> approval gate
  -> idempotency lookup
  -> timeout/retry execution
  -> idempotency write
  -> lifecycle event
```

Unknown tools are rejected by the registry. Approval defaults to unavailable rather
than implicitly allowing an action.
