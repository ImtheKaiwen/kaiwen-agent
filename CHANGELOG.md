# Changelog

## Unreleased

- Add reviewer assessment and host-controlled team acceptance (M6.3.4, ADR 0017 Proposed):
  `AgentRuntimeWorker(review=ReviewTools())` with the strict, bounded `review.submit` tool, finalized
  into the reviewer's own task output and bound by the host to run, owner, workspace, its task id,
  per-task output digests and the fingerprint; opt-in `WorkflowController(team_acceptance=
  TeamAcceptance(...))` evaluated after a passing completion gate, returning `accepted`,
  `changes_required`, `approval_required`, `blocked` or `unavailable` (`WorkflowOutcome.team_acceptance`,
  block reason `team_<status>:<code>`); `HumanApprovalPort` for a host-supplied human decision bound to
  a digest of the exact delivery. `proven_cycle` is now public. No new table; the
  default behavior is unchanged; approval-required delegation stays blocked. Audit R1: acceptance
  re-checks the protected fingerprint right before completion (`workspace_changed_after_acceptance`),
  a review records the proven cycle (same-cycle dependency reviews are valid), acceptance evidence
  must be verified with the completion gate's artifact manager and signer, and an approval cannot
  leave a criterion unknown.
- Add bounded earlier-cycle task handoffs (M6.3.3-C3, ADR 0015): opt-in `CrossCycleHandoffs` for
  `TaskContextAssembler` and `AgentRuntimeWorker(cross_cycle=..., evidence_signer=...)`. A task with
  a compiler-written delegation parent receives informational `TaskHandoff`s (new optional fields
  `origin`, `source_cycle`, `source_worker_id`, `relation`) from its parent, the parent's completed
  delegation-round siblings and its grandparent, chosen by the host from one bounded `TaskRunQuery`
  scan in task-id order. Sources must belong to the run and owner, be COMPLETED, sit in a strictly
  earlier cycle and carry a compiler-derived id (`compiled_task_id`, now public). Artifact and
  evidence references are re-verified with the existing rules. `TaskContextPackage` gains a
  `cross_cycle` summary (`complete`, `truncated`, `unavailable` with a stable reason,
  `not_applicable`); no graph edge, table, transcript, permission or capability is added, and the
  default behavior is unchanged. Audit R1: the cross-cycle source must be the assembler's own
  task store (or the `FencedTaskStore` wrapping it), refused at construction in both the
  assembler and `AgentRuntimeWorker`; a selected source without an output record, or an
  unprovable row in the lineage position, now makes the summary `unavailable`
  (`source_output_missing`, `source_identity_invalid`) instead of `complete`, and a valid task of
  another delegation round is no longer counted as rejected. Audit R2: only the exact platform
  `FencedTaskStore` type is accepted as the fenced wrapper of the assembler's store; a subclass
  that could override `list_run_tasks` is refused like any foreign object.
- Add model-facing Lead tools and durable delegation proposals (M6.3.3-C2, ADR 0015): registered
  `lead.view` (bounded, paginated Lead coordination view) and `lead.propose_task` tools built by
  `LeadTools` over host `LeadToolConfig` role bindings; `AgentRuntimeWorker(lead=...)` allocates a
  fresh execution-local proposal sink per execution and, after a successful run, returns the
  canonical, digested proposal set in the Lead task's output so the existing scheduler commits it
  atomically with COMPLETED (through the fenced task store in fenced mode);
  `DurableLeadProposalSource` is a `WorkflowDelegationSource` that re-reads and validates that
  record at the cycle boundary. Proposal identity ignores the tool-call id; identical proposals
  are idempotent and changed content under one logical id is rejected. The controller's fresh
  final admission is unchanged. Additive and opt-in: workers without `lead=` behave as before.
  Also fixes `TaskScheduler.run`, which could return while another task's final save was still
  in flight (a worker marks its task COMPLETED in memory before saving), so a reader of the
  store saw a stale row; it now returns only after every worker has finished, and a refused
  final write surfaces at once instead of waiting for another hung write.
  Audit R1: `WorkflowController` refuses a `DurableLeadProposalSource` over another task store,
  run manager or workspace, a Lead worker the source does not read, or a Lead tool configuration
  that differs from the source's; `AgentRuntimeWorker` requires its own registered Lead tool
  objects and permissions; every element of a proposal is length-bounded, on staging and on read.
- Add opt-in cycle-boundary delegation to `WorkflowController` (M6.3.3-C1, ADR 0015):
  `WorkflowController(delegation=WorkflowDelegation(source, admission, max_rounds))`, a new
  `WorkflowRevisionReason.DELEGATION_REQUESTED` (with typed `DelegationBoundary` data on
  `WorkflowRevisionRequest`, which can carry no failure and no gate result), `DelegationCandidate`
  and `WorkflowDelegationSource`. After a cycle with no failures and before the completion gate, a
  host-controlled source may propose work; only a task completed in that cycle may be the parent,
  and the proposal is freshly authorized (run, workspace, host `DelegationAuthority`, ceiling,
  child worker, central policy with live counters, compiler) before the controller compiles
  exactly the admitted tasks as the next cycle. Anything short of an explicit allow blocks the run
  with an explicit reason. `WorkflowCompiler.compile` gains an optional `DelegationBinding` that
  writes `parent_id` and `delegation_depth` on delegated tasks, and workflow admission now also
  checks the child worker's declared `allowed_capabilities`. Delegation requires fenced execution
  (`fenced=`, ADR 0016): Redis, in-memory or custom stores, a second database, an admission over
  other runs or another task database, and unfenced schedulers are refused at construction, and a
  stale or expired owner is refused at the first delegated task write.
  Workflow admission denies an incomplete task listing (`state_incomplete`), and the controller
  asks the lease store whether it still holds the lease before and after delegated work is
  authorized, because the run record's stored token does not prove it. Without `delegation`
  configured nothing changes. No durable proposal storage yet (PR-C2).
- Add opt-in fenced execution over one SQLite database (ADR 0016): `SQLiteLeaseStore` (transactional
  lease acquisition with durable, never-reset epochs in the protected database), `OwnerEpoch`,
  `SQLitePersistence.save_task_fenced`, epoch-checked run transitions with monotonic ownership
  publication (`RuntimeRunRecord.fenced`), epoch-checked action-intent writes including
  PREPARED to EXECUTING, `FencedTaskStore`, `FencedActionIntents` and
  `FencedExecution` / `WorkflowController(fenced=...)`. Stale or expired owners are refused with
  `OwnershipLostError`; the scheduler stops, cancels its workers and records nothing after a
  refusal. Unsupported configurations (Redis or in-memory leases, custom stores, a second
  database, unfenced schedulers) are refused at construction. Existing workflows and stores are
  unchanged; effects are not exactly-once. `SQLitePersistence` gains `busy_timeout_seconds`.
  Audit corrections: lease expiry is judged at the protected transaction, not at an earlier clock
  sample; a fenced run keeps its flag and accepts no ordinary transition or action-intent
  finalization while RUNNING; `RuntimeRecoveryManager` gains a narrow, lease-backed authority that
  can only block such a run and mark an EXECUTING action uncertain.
  The published `owner_lease_key` and `owner_fencing_token` of a fenced run can no longer be
  rewritten outside a fenced start (previously an ordinary update of an idle fenced run could).
- Add shared, host-controlled delegation admission (M6.3.3-B, ADR 0015 accepted):
  `DelegationAdmission`, `DelegationClaim`, `AdmissionDecision` and `AdmissionOutcome` in
  `kaiwen_agent.policy`. It reuses the central `PolicyEngine` (`agent.delegate`); only an
  explicit allow admits, and require-approval, review and modify never admit or rewrite a
  request. Decisions are preliminary and point-in-time (`preliminary` and `reserves_nothing`
  are always true), not a reservation or a final authorization. `SubagentDelegator` is rebuilt
  on it with its public behavior kept; a policy that raises, or malformed host facts, now fail
  closed with a stable message and no exception text instead of propagating the raw exception.
  `SubagentDelegator(resource_allowlist=...)` is a new opt-in that denies delegated
  `resource_keys` outside a host-owned list; the default stays unrestricted. New
  `WorkflowDelegationAdmission` and `WorkflowDelegationProposal` validate a Lead's workflow
  proposal against the trusted run, workspace, parent task (depth derived by the host), the
  host-owned `DelegationAuthority` mapping that says which workers may delegate and up to which
  capabilities (a parent task's recorded capabilities are plan-authored and never the
  authority), the `WorkflowCompiler` allowlists and limits and the shared admission, without
  creating tasks, submitting jobs or running the scheduler. The bounded task scan of the Lead view moved to the
  shared `scan_run_tasks` with unchanged behavior.
- Add the read-only Lead coordination view foundation (M6.3.3-A, ADR 0015 proposed): the
  optional `TaskRunQuery` store capability (`list_run_tasks`, owner- and run-scoped, SQL-filtered,
  keyset-paged, with `SQLitePersistence` support), and `LeadViewAssembler`/`LeadView`, a bounded,
  provenance-labelled projection of one durable run that writes nothing and fails closed without
  the capability. `SQLitePersistence` adds nullable ownership columns and an index to `tasks`;
  existing rows are checked once against `runtime_runs` (membership of the claimed run, not proof
  of who wrote the task) and rows that fail the check stay unavailable to run-scoped queries.
  `TaskContextAssembler.owned_references` is now public. The view stays bounded however a custom
  `TaskRunQuery` behaves (a page that violates the contract raises `LeadViewError`
  `task_listing_invalid`), storage failures surface as stable `LeadViewError` codes or
  `unavailable_sources` without raw exception text in values or logs, and reference
  availability is explicit per task (`artifact_check`, `evidence_check`, `unchecked_reference_count`).
- Fix durable action ownership for workflow workers: action intents and approvals of tools run
  by `AgentRuntimeWorker` were stored under the per-invocation `AgentRun` id, so
  `WorkflowController` cancellation and `RuntimeRecoveryManager` could not associate an in-flight
  action with its durable run (a cancelled run could be recorded as safely cancelled while a side
  effect was in flight). They are now owned by the durable workflow run through a host-verified
  `DurableActionScope` (new optional `action_scope` on `Agent.run`, `AgentRuntime.run/action` and
  `ToolExecutor.execute`), with the task as part of the idempotency identity. Standalone
  executions are unchanged. Records written earlier are not rebound; `RecoveryReport` gains
  `unassociated_action_ids` for them.
  `WorkflowController(block_unassociated_actions=True)` (opt-in) blocks runs of an owner with an
  unresolved action no known run owns, until an operator resolves it. `ToolResultProcessingExecutor`
  forwards `action_scope` only when set (older `ToolExecutor` subclasses keep working; a scope is
  never dropped silently), and offloaded tool-result artifacts of workflow workers are owned by
  the durable run.
  Durable-scope support is now an explicit executor capability (`supports_action_scope`,
  `executor_supports_action_scope`): `**kwargs` no longer counts, and an executor that does not
  declare it is refused before any tool runs when a scope is supplied. `WorkflowController`
  re-blocks a restarted run that still has an executing or uncertain action of its own until an
  operator resolves it. `RuntimeRecoveryManager.inventory_unassociated_actions` lists every
  unresolved action of an owner that no known run owns (`RecoveryReport` only lists actions
  that pass found executing).
- Fix the OpenAI function-tool serializer: strict mode is now chosen per tool. A tool whose input
  schema contains a free-form mapping (for example the Coding Kit command tools' `parameters:
  dict[str, str]`) is sent with `strict: false` and its original schema, because OpenAI strict
  schemas require `additionalProperties: false` on every object; strict-compatible tools stay
  strict. Host-side argument validation is unchanged. Add an opt-in live validation harness
  (`scripts/live_validation.py`) with sanitized provider diagnostics.
- Add `AgentRuntimeWorker`, which runs a configured `AgentRuntime` as a workflow `Worker` over
  the existing scheduler with host-owned permissions and tools, a bounded handoff-shaped result
  (`verified: false`) and private per-task conversation ids. Add `WorkspaceBinding` stored in the
  durable run record (`RuntimeRunRecord.workspace_binding`, optional, write-once;
  `DurableRunManager.bind_workspace`, `WorkflowController(workspace_binding=...)`),
  `AuthorizedCodingWorkspace` (mandatory fresh-fingerprint and pipeline consistency for coding
  workers), `TaskContextPackage`, `TaskContextAssembler` and `TaskHandoff` (bounded, trust
  separated, owner- and run-scoped reference handling; evidence references are authenticated
  with the existing `EvidenceSigner`, never by a label). Coding workers that produce evidence
  require a controller and a host-assigned workspace identity (`identity_digest` on the
  binding). Existing workflows are unchanged. See ADR 0014.
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
