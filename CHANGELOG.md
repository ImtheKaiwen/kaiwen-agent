# Changelog

## Unreleased

- Add `AgentRuntimeWorker`, which runs a configured `AgentRuntime` as a workflow `Worker` over
  the existing scheduler with host-owned permissions and tools, a bounded handoff-shaped result
  (`verified: false`) and private per-task conversation ids. Add `WorkspaceBinding` stored in the
  durable run record (`RuntimeRunRecord.workspace_binding`, optional, write-once;
  `DurableRunManager.bind_workspace`, `WorkflowController(workspace_binding=...)`),
  `AuthorizedCodingWorkspace` (mandatory fresh-fingerprint and pipeline consistency for coding
  workers), `TaskContextPackage`, `TaskContextAssembler` and `TaskHandoff` (bounded, trust
  separated, owner-scoped reference verification). Existing workflows are unchanged. See
  ADR 0014.
- Add `WorkflowController(fingerprint_provider=...)` so completion is evaluated against a freshly
  obtained, content-aware project fingerprint at every gate evaluation (a passing result is
  re-checked and a failing or empty provider blocks the run), `WorkspaceFingerprintProvider`,
  `SnapshotBuilder.fingerprint()`, `trusted_task_context` for run-identity-checked worker
  contexts, `TrustedEvidencePipeline`, and `WorkflowOutcome.verified_input_fingerprint`.
  Workflows that pass a fixed fingerprint behave as before. Fix a reader-task exception left
  unretrieved when a sandboxed command timed out. See ADR 0013.
- Add `DockerSandboxRunner`, a first-party `SandboxRunner` that runs preset commands in a
  locked-down container (digest-pinned image, non-root, no network, dropped capabilities,
  read-only root, CPU/memory/process/time/output limits, no host fallback) over a private
  `ExecutionSnapshot` with a content-aware fingerprint. Add `CommandEvidenceRecorder` and
  signed evidence verification so real test results feed the completion gate; failed,
  timed-out, stale, or unsigned evidence never passes. `testing_tools` can run test, lint
  and typecheck presets without approval only for an isolating runner. `CommandCatalog`
  gains optional observers and `SandboxResult` gains optional snapshot and image fields.
  Evidence is recorded only for runners the host names (`trusted_runners`), runs that
  change their own snapshot files never pass, and the per-run directory is owner-only.
  The container user defaults to the non-root host uid so run directories can be removed
  (failures set `SandboxResult.cleanup_failed`), and sandbox output is held to its byte
  budget after UTF-8 decoding. Evidence now records `cleanup_failed`, and a failed or
  unreported cleanup blocks the completion gate. See ADR 0012 and `docs/coding-kits/PERMISSIONS_AND_SANDBOX.md`.
- Fix recent-conversation context freezing after 1,000 stored messages, context budgets
  dropping the newest turns, and conversation compaction stalling after the first 1,000
  messages. Add the optional `ConversationWindowStore` capability
  (`list_latest_messages`, `message_exists`), implemented by the in-memory and SQLite
  stores, and an optional `ContextItem.order`. Existing `ConversationStore` behavior,
  including `after_id` reads, is unchanged.
- Add `run.cancelled` (Python, JSON Schema, TypeScript, UI state) and the `cancelled`
  `AgentRun` status. Cancelling an agent run, scheduler, workflow, or job now ends in a
  terminal cancelled state, stops workers, and releases resources; an interrupted
  durable side effect becomes uncertain and is never replayed.
- Fix `JobManager.wait()` swallowing its caller's cancellation, and jobs failing instead
  of waiting when they share a resource with another job.
- Reject reuse of an idempotency key with different arguments and run identical
  concurrent side-effecting calls once. The action-intent id no longer includes the
  arguments checksum; intents persisted by earlier versions are adopted by owner, run,
  and idempotency key, so their receipts, uncertainty, and approvals are preserved and
  they are never executed again. See the upgrade procedure in `docs/persistence.md`.
- `AgentRuntime` requires a durable action-intent store for autonomous side-effecting
  tools (`DurableSideEffectsRequiredError`).
- Add opt-in `tool_errors="return"` with bounded sanitized tool results
  (`ToolResult.is_error`); fail-fast remains the default.
- Raise `ModelResponseError` for malformed OpenAI tool arguments, classify transient
  provider errors for failover by default, and add `timeout` and `max_retries` to the
  OpenAI adapter and `timeout` to the Ollama adapter.
- Add optional run ownership leases to `WorkflowController`, and make
  `RuntimeRecoveryManager` block runs whose lease owner is gone (`owner_lost`).
- `CodingKit` retains its background run tasks and logs their failures.
- Make run-state writes conditional on the stored status and the owner's fencing
  token, so a stale recovery snapshot or a stale executor cannot overwrite a newer
  state. `RuntimeRunStore.save_runtime_run` gains optional `expected_status` and
  `expected_owner_fence` arguments; custom stores that lack them are not used for
  owner-lost blocking and cannot enforce ownership.
- Cancellation uses a complete run-scoped intent query and blocks the run when the
  lookup is incomplete or fails, instead of recording a possibly unsafe `CANCELLED`.

- Security: workspace `read_text`, `write_text`, and `apply_patch` now enforce the
  ignore and sensitive-path policy (`.env*`, private keys, `.git`, ignored
  directories). Matching is case-insensitive and normalizes Windows aliases, and
  `.git` stays protected when a host supplies its own ignore list.
- Security: read-only Git tools reject `rev:path` revisions, apply the path policy to
  their `path` argument, and omit protected files, including host-configured ignored
  directories, from diff, show, and status output.
- Security: read-only Git tools no longer execute programs named by repository
  configuration (`core.fsmonitor`, text converters, clean and smudge filters, external
  diff commands) and no longer traverse submodules.

## 0.7.0a1 — 2026-10-18

- Add the optional `CodingKit` Simple Coding API for lazy local or authenticated-host
  chat, explicit allowlisted actions, durable solo workflows and explicitly bound
  team workflows without replacing existing runtime authorities.
- Add owner/workspace-scoped durable run handles, truthful lifecycle capability
  errors, provider/tool/locality/sandbox/approval preflight, and runnable deterministic
  consumer examples.

## 0.6.0a1 — 2026-10-17

- Add opt-in first-party Coding Kits with packaged English role prompts, adaptive
  standards, evidence verifiers, bounded project discovery and proportionate docs.
- Add safe read/patch/Git/command toolkits, with approval-gated writes and injected
  sandbox execution instead of an unrestricted host shell.
- Add `CodingAgent` and adaptive `CodingTeam` composition over existing runtime,
  workflow, team, policy, recovery, context and observability systems.
- Add provider capability/locality preflight for host-registered OpenAI, Gemini and
  Ollama configurations without embedding credentials or claiming unrun live tests.

## 0.5.0a1 — 2026-10-17

- Add deterministic lifecycle, central policy, bounded context, short-term and
  policy-gated long-term memory foundations.
- Add background jobs over the existing Task Runtime, bounded subagent delegation,
  trigger routing, objectives, watches, and attention evaluation.
- Add policy-gated model routing, atomic hierarchical budgets, immutable prompt and
  skill registries, artifacts, and capability manifests.
- Add evaluation, observation-only replay, redacted dead letters, backpressure,
  health, server-side secret providers, distributed leases, and tenant-scoped
  metrics.
- Add optional PostgreSQL/pgvector memory and Redis coordination adapters.
- Add a compatibility-preserving, single-agent-first `AgentRuntime` with explicit
  chat/action modes, independent autonomy levels, trusted identity propagation, and
  deny-by-default tool exposure.
- Add immutable agent definitions and workspace profiles, a provider factory registry,
  fail-fast composition validation, and exact runtime configuration pins.
- Add optional bounded runtime context composition, provider-aware token counters,
  provenance-checked rolling compaction, and owner-scoped tool-output offloading.
- Add versioned standards packs, deterministic requirement resolution, host verifier
  registration, evidence-backed completion gates, and bounded repair tracking.
- Add owner-scoped durable workflow records with immutable configuration pins,
  restart-safe action intents, one-time approvals, SQLite idempotency receipts, safe
  lifecycle controls, and lease-guarded uncertain-action recovery.
- Add validated single-agent workflow plans compiled into the existing Task Runtime,
  with owner isolation, evidence-backed completion, and finite repair/replan control.
- Add optional versioned team composition over the existing delegation and job
  runtime, with least-privilege member selection and bounded structured handoffs.
- Add technical model capability records, fail-closed data-locality routing, and
  optional Gemini/Ollama adapters with centrally executed tool proposals.
- Add provider-neutral speech recognition, synthesis, transcript, and ephemeral
  pipeline contracts with explicit audio-processing permission and deterministic fakes.
- Add bounded workspace filesystem tools and trusted MCP tool bindings that preserve
  central permissions, approvals, side-effect classification, and execution policy.
- Add credential-free runtime and speech quickstarts that execute in CI.
- Redact canonical durable event payloads at validation and built-in persistence or
  streaming boundaries, with an MCP toolset-escalation injection regression.
- Add a deterministic concurrent-run regression proving provider continuation state
  remains local to each agent run.
- Add backward-compatible event owner fields and exact tenant/user/session scoped
  reads, including a fail-closed SQLite migration for historical unowned events.
- Add policy-approved initial-call model failover with locality revalidation, explicit
  transient-error classification, and strict provider continuation affinity.
- Add CI-verified, credential-free autonomous workflow and least-privilege team
  quickstarts.
- Add a concurrent multitenant regression for exact event visibility, action-intent
  identity, and restart-safe receipt replay under deliberately colliding client IDs.
- Add a real subprocess-boundary checkpoint recovery regression and installed-wheel
  consumer validations for an isolated Todo workflow and a policy-bounded fictional
  legal domain.
- Require formatter checks and the isolated consumer validations in CI and release
  verification.

## 0.4.0a1 — 2026-09-24

- Add provider-neutral inbound phone-call contracts and retry-safe webhook decision
  deduplication.
- Add the OpenAI Live direct-SIP accept, reject, transfer, and hangup adapter.
- Add a Twilio outbound PSTN-to-SIP adapter with TLS/SRTP-ready SIP targets and
  allowlisted custom SIP binding headers.
- Convert accepted SIP calls into normal phone-channel realtime session records so
  existing sideband tools, permissions, approvals, idempotency, and audit can be reused.
- Parse only already-verified `live.transport.incoming` events and keep SIP headers as
  untrusted metadata.
- Normalize received DTMF digits without treating them as client commands.

## 0.3.0a1 — 2026-09-18

- Add trusted OpenAI Live sideband attachment for existing browser WebRTC sessions.
- Register strict Responses-delegation tool schemas when creating Live sessions.
- Execute completed delegated function calls through the existing permission,
  approval, idempotency, retry, and audit pipeline.
- Bind sideband tool execution to the logical realtime session, tenant, and user.
- Keep sideband shutdown independent from primary WebRTC/SIP media and reject
  duplicate completed response events.

## 0.2.0a1 — 2026-09-18

- Add the provider-neutral Realtime Core with isolated session lifecycle,
  interruption/reconnect handling, and layered concurrency limits.
- Add the OpenAI Live primary WebSocket adapter and server-authorized WebRTC session
  bootstrap without exposing API credentials to clients.
- Add normalized audio, transcript, usage, error, and nested Responses events.
- Add tenant-scoped durable conversations that unify text and voice transcripts while
  keeping raw audio ephemeral.
- Add allowlisted background task submission and tenant/user-scoped task status.
- Extend `AgentContext` and `AgentTask` with optional tenant, user, client, and
  conversation identity fields.
- Add SQLite conversation persistence, fake transports, integration tests, and
  reusable Realtime/Live documentation.

## 0.1.0a2 — 2026-09-17

- Fix multi-step OpenAI tool execution when `store=False`.
- Carry provider-owned continuation state inside a single agent run without sharing
  conversation state between sessions.
- Preserve encrypted reasoning items for stateless Responses API continuation.
- Expand the OpenAI smoke example to verify a real two-turn tool call.

## 0.1.0a1 — 2026-09-17

- Initial alpha release of the Python runtime and OpenAI adapter.
- Initial npm releases of the shared protocol and headless UI runtime.
