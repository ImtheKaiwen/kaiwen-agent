# Delivery roadmap

The detailed architecture lives outside product applications. This repository tracks
implementation status for the reusable packages.

Status markers: `[x]` completed, `[~]` in progress, `[ ]` planned, `[!]` blocked.
This file records verified progress only. Entries marked completed reflect the
changelog, accepted ADRs, and repository contents; they are not a statement that
tests were re-run when the entry was last edited. Dated reports under
`docs/validation/` and `docs/coding-kits/` are historical snapshots.

## Completed

- [x] Phase A: repository boundaries, canonical JSON Schema, shared fixtures, CI.
- [x] Phase B: async Python core, fake provider, tool registry, in-memory stores.
- [x] Phase C: OpenAI Responses adapter, strict function schemas, structured output,
      permissions, approvals, idempotency, timeout/retry classification, usage events,
      and provider contract tests.
- [x] Phase D: session/run/event/audit ports, SQLite reference persistence, PostgreSQL
      adapter outline, SSE fan-out, trace propagation, and secret redaction.
- [x] Phase E: durable task state machine, DAG validation, scheduler, workers,
      dependencies, deterministic retry/cancellation, mailbox/checkpoints, resource
      locks, and global/per-agent concurrency limits.
- [x] Phase F: headless TypeScript UI runtime, semantic action registry, HTTP/SSE
      transport, capability discovery, React bindings, artifact renderers, and
      Dynamic Island state machine.
- [x] Phase R1: provider-neutral Realtime Core, explicit session lifecycle,
      tenant/user isolation, concurrent session limits, connection primitives, and
      deterministic fake provider.
- [x] Phase R2: OpenAI Live primary WebSocket adapter, server-authorized WebRTC
      bootstrap, text/audio commands, barge-in readiness, event normalization, and
      transport contract tests.
- [x] Phase R3: tenant-scoped conversation messages, text/voice transcript assembly,
      SQLite persistence, allowlisted task submission, and task status isolation.
- [x] Phase R2.1: trusted backend sideband attachment for existing browser WebRTC
      sessions, strict delegated tool schemas, identity binding, and safe tool
      execution without restarting the Live session or sending primary audio.
- [x] Phase R8: provider-neutral inbound phone contracts, retry-safe webhook decision
      claims, OpenAI Live direct-SIP controls, Twilio PSTN-to-SIP origination,
      phone-channel realtime records, and DTMF normalization. Provider provisioning
      and product routing remain consumer deployment concerns; scheduled objectives
      and automatic calls remain R9.
- [x] Phase R5A.1: deterministic lifecycle skeleton, durable wakeup/cycle/runtime
      ports, atomic due-wakeup claims, in-memory and SQLite reference stores,
      framework-neutral runtime controls, lifecycle event contracts, and a host
      scheduler adapter. Model reasoning, policy-controlled self-scheduling, and
      product HTTP routes remain later integration work.
- [x] Phase R5B.1: central policy decision model and registry, deterministic
      precedence, capability/runtime/scheduling/delegation policies, existing tool
      permission and approval adapters, policy audit/events, and fail-closed tool
      integration. Product policy definitions remain consumer-owned.
- [x] Phase R5C.1: bounded context builder, prioritized source budgets, recent
      conversation and session-summary adapters, short-term session summary
      retention, in-memory and SQLite summary stores, and explicit forget support.
      Long-term admission, semantic memory, and model-generated summarization remain
      later phases.
- [x] Phase R5D.1: long-term memory models, secret-first candidate admission,
      policy-gated user/project namespaces, duplicate and conflict handling,
      review inbox, bounded hybrid retrieval, edit/forget controls, a framework-neutral
      admin contract, deterministic in-memory storage, and an optional PostgreSQL +
      pgvector production adapter. Model-driven extraction, background consolidation,
      shared-project ACL composition, and retention workers remain later work.
- [x] Phase R5F.1: non-blocking background job submission over the existing Task
      Runtime, immutable job/task/trace IDs, durable in-memory and SQLite job stores,
      progress/result events, cooperative cancellation, parent-child progress, and
      explicit restart recovery. Distributed leases, stuck-job detection, and
      model-driven delegation remain later phases.
- [x] Phase R5F.2: fail-closed subagent delegation over the shared JobManager,
      strict capability subset inheritance, central allowlist/depth/parallel/per-run
      policy limits, and shared text/realtime bridges. Approval workflow composition
      for delegation remains host-owned.
- [x] Phase R5H.1: deterministic trigger filtering, tenant-aware rules, replay
      deduplication, bounded debounce/coalescing, lifecycle wakeup delivery, and
      canonical trigger events. Durable distributed trigger coordination remains R7.
- [x] Phase R5I.1 / R5J.1 / R5K.1: persistent objective state machine,
      structured event/webhook/polling watches, watch-driven objective resume and
      lifecycle wakeups, deterministic attention severity, optional host notification
      port, and SQLite reference persistence. Distributed claims and notification
      delivery policy remain production composition work.
- [x] Phase R6A-R6F.1: policy-gated provider-neutral model routing, hierarchical
      token/cost/tool/job budgets with restart-safe SQLite reference accounting,
      immutable prompt and declarative skill registries, owner-scoped bounded
      artifacts, and versioned capability manifests. Distributed atomic budget
      reservations, product definitions, and production object storage remain host
      or R7 concerns.
- [x] Phase R7A-R7F.1: structured evaluation suites, observation-only event replay,
      fail-closed simulation policy, redacted tenant-scoped DLQ with SQLite reference
      persistence, process-local backpressure leases, heartbeat-derived health, and
      an allowlisted server-side secret-provider abstraction. Distributed admission,
      durable production probes, hosted vault adapters, load tests, and operational
      alerting remain production composition work.
- [x] Phase R7 production-hardening slice: atomic SQLite multi-scope budget
      reservations, provider-neutral distributed lease contracts, an optional Redis
      lease adapter with fencing tokens, job-result lifecycle wakeups, and failed-job
      DLQ composition, plus a fixed-catalog tenant-scoped metrics port. Distributed
      rate admission, live Redis/load validation, production metric exporters, and
      deployment-specific fencing enforcement remain production work.

## Next

- [x] Phase U0: freeze legacy public entry points and accept the single-agent-first,
      standards-separation, and safe-boundary recovery architecture decisions.
- [x] Phase U1: minimal `AgentRuntime` composition with explicit mode/autonomy,
      trusted identity, chat fast path, and bounded synchronous action path.
- [x] Phase U2: immutable agent/workspace definitions, provider composition,
      fail-fast capability validation, and exact in-run version pins.
- [x] Phase U3: optional runtime context integration, provider-aware token accounting,
      structured rolling compaction, and owner-scoped large tool-output offloading.
- [x] Phase U4: versioned standards resolution, verifier registration, owner-scoped
      evidence gates, and bounded repair/stagnation models.
- [x] Phase U5: owner-scoped durable runtime state and immutable pins, stable action
      intents, restart-safe one-time approvals and receipts, safe lifecycle controls,
      and lease-guarded reconciliation that blocks uncertain external effects.
- [x] Phase U6: bounded autonomous single-agent workflows compiled to the existing
      Task Runtime, with evidence gates and finite repair/replan decisions.
- [x] Phase U7: optional versioned teams over the existing delegation, JobManager,
      and Task Runtime boundaries, with deterministic profile selection and bounded
      structured handoff.
- [x] Phase U8: technical model capabilities, data-locality-aware routing, and
      optional Gemini/Ollama adapters that return tool proposals without executing
      tools inside provider SDKs.
- [x] Phase U9 speech slice: provider-neutral STT/TTS and transcript contracts,
      explicit audio-processing consent, an ephemeral pipeline, and deterministic
      fake adapters. Heavy speech engines and ecosystem tools remain optional.
- [x] Phase U9 ecosystem foundation: host-bounded workspace filesystem tools and
      trusted MCP bindings that route through the central tool safety pipeline.
- [x] Phase U10 DX foundation: credential-free simple-runtime, bounded autonomous
      single-agent, least-privilege team, and speech examples whose imports and
      execution are continuously verified by CI.
- [x] Phase U10 security foundation: durable event payload redaction at validation,
      built-in persistence/stream defense in depth, and an MCP prompt-injection
      regression proving that untrusted output cannot expand the registered toolset,
      concurrent-run provider continuation isolation, and exact owner-scoped event
      reads with fail-closed legacy migration. A concurrent SQLite matrix also proves
      same-ID tenant/user isolation across events, action intents, and durable replays.
- [ ] Phase U10 hardening: bounded initial-call provider failover now preserves
      locality and provider-state affinity; optional document and specialized adapters,
      the remaining crash/multitenant/load matrices, and expanded developer
      documentation remain.

- [x] Phase K1 (0.6.0a1): opt-in first-party Coding Kits under `kaiwen_agent.kits`:
      packaged role prompts, adaptive standards, evidence verifiers, bounded project
      discovery, read/patch/Git/command toolkits with approval-gated writes, and
      `CodingAgent` / `CodingTeam` composition over existing runtime systems
      (ADR 0008). Command execution requires a host-supplied sandbox runner; no
      first-party isolated runner ships.
- [x] Phase K2 (0.7.0a1): thin Simple Coding API facade (`CodingKit`) with
      owner-scoped durable run handles, capability preflight, and deterministic
      consumer examples (ADR 0009).

- [ ] Phase R4 / G: authenticated portfolio admin integration, WebRTC bootstrap route,
      Dynamic Island voice states, task notifications, and semantic UI dispatch.
- [ ] Phase R5A.2: policy-controlled lifecycle actions, reasoning integration, and
      production scheduler composition.
- [ ] Phase R5D.2: observable candidate extraction, consolidation/decay jobs,
      shared-project ACL composition, retention enforcement, and memory evaluations.
- [ ] Phase R6.2: production tenant isolation composition, multi-node quota
      reservations, policy configuration, and tenant-scoped observability.
- [ ] Phase R7: remaining production scaling, distributed rate admission, recovery,
      live-adapter load tests, and operational alerting.
- [ ] Phase R9: scheduled/objective calls, daily reports, explicit consent, and call
      summaries written through the memory policy.
- [ ] Phase R10: security hardening, abuse controls, retention enforcement, and
      production readiness review.

## Repository milestones (M1-M6.2)

Each milestone requires explicit owner approval before it starts. Milestones are
small, independently reviewable changes with their own acceptance criteria.

### M1: Repository and documentation foundation (completed)

- [x] Add `CLAUDE.md`, retire `docs/AI_CONTEXT.md` into `docs/architecture.md`,
      move the three legacy Turkish planning documents to `docs/archive/`, refresh
      `README.md` and this roadmap, label dated reports as historical snapshots.
- Acceptance: no broken relative Markdown links; no production source, test,
  package manifest, or workflow changes; no emojis introduced in new or modified
  documentation (pre-existing content in archived documents is preserved unchanged);
  changes reviewed and accepted by the owner. All criteria were verified on
  2026-11-07 by link check, diff inspection, and hash comparison; no tests were run
  because no code changed.

### M2: Root-level Python restructuring (completed)

- [x] `backend/src`, `backend/tests`, and `backend/pyproject.toml` moved to the
      repository root with Git-aware renames; `backend/README.md` consolidated into
      the root README (links are absolute so the PyPI long description renders);
      the `backend/` directory removed. Distribution name and version are unchanged
      (`0.7.0a1`); the sdist is limited to `src/kaiwen_agent`, `tests`, `README.md`,
      `LICENSE`, and `pyproject.toml`.
- Merged as pull request 2 (squash commit `4441515`) after exact-head CI passed
  (Python 3.12 on Linux and Node 22). Local verification before merge: pytest
  `282 passed, 1 skipped`, identical to the pre-move baseline; Ruff, mypy, examples,
  wheel and sdist builds, `twine check`, and the npm scripts passed. The wheel's
  package files and contents are identical to a pre-move build; `METADATA` differs
  only in the long description and a `licenses/LICENSE` entry was added.

### M3: CI/CD and packaging hardening (code complete; owner configuration pending)

- [x] Reusable `verify.yml` shared by `ci.yml` and `release.yml`: Ruff, mypy, pytest
      on Python 3.11, 3.12, and 3.13 (Linux) and 3.11 and 3.13 (Windows), executable
      examples, Node 20 and 22, wheel and sdist build, `twine check --strict`,
      content checks (`py.typed`, coding-kit prompts, sdist limited to intended
      paths), clean-environment install of both artifacts, `npm run pack:check`.
      A single `CI gate` status is provided for branch protection.
- [x] Release: runs only from `main`, publishes the exact artifacts produced by the
      verification run (no rebuild), `id-token: write` only on the two publish jobs,
      every third-party Action pinned to a full commit SHA, npm pinned to 11.5.1.
- [x] `scripts/check_release.py` checks version relationships, the changelog heading,
      and distribution contents. Python and npm versions remain independent.
- [x] Repository URLs in `pyproject.toml` and both `package.json` files corrected to
      `ImtheKaiwen/kaiwen-agent`.
- [x] Merged as pull request 3 (squash commit `c760659`) after exact-head CI passed
      (14 jobs: Python 3.11 to 3.13 on Linux, 3.11 and 3.13 on Windows, Node 20 and
      22, package, quality, gate).
- [!] Owner actions (remote settings, not changed by this work): ruleset on `main`
      requiring a pull request and the `CI gate` check; required reviewers and a
      `main`-only deployment branch policy on the `npm` and `pypi` environments;
      "Require actions to be pinned to a full-length commit SHA"; confirm PyPI and npm
      trusted-publisher entries use repository `kaiwen-agent`. See
      `docs/releasing.md`. A read-only query on 2026-11-07 found none of these in
      place and no ability to read the trusted-publisher configuration.
- Observed on Windows CI: examples that print non-ASCII text fail under the default
  cp1252 stdout encoding when output is redirected (for example
  `examples/coding-simple-chat`). CI sets `PYTHONUTF8=1` for the example step; the
  examples are unchanged.
- Observed release inconsistencies, not changed: no Git tags for 0.4.0a1 or 0.6.0a1
  although PyPI has them; GitHub releases stop at 0.5.0a1 and mark 0.3.0a1 as
  "Latest"; no tags for npm releases.
- Not verified: the publish jobs (npm tarball publishing with provenance and the
  PyPI artifact upload have not been exercised, by design), Python 3.11 and 3.13
  locally.

### M4: Security hardening (completed)

Scope: reproduce concrete defects with failing tests, then fix them in the existing
implementation. No second policy engine or tool pipeline was added.

- [x] Reproduced and fixed: `WorkspaceFilesystem.read_text` and `write_text` bypassed
      the ignore and sensitive-path policy, exposing `.env`, keys, and `.git` through
      `workspace.read_text` and `workspace.apply_patch`, and allowing writes to
      `.git/hooks`. 33 of the first 37 new tests failed before the fix.
- [x] Reproduced and fixed: ignored-name matching was case-sensitive and did not
      normalize Windows aliases (`.GIT`, `.git.`, `.env:stream`); a host-supplied
      ignore list removed `.git` protection; `.npmrc`, `.netrc`, `.pypirc`, SSH keys,
      `.pfx`, and `.jks` were not treated as sensitive.
- [x] Reproduced and fixed: the read-only Git tools returned protected blobs through
      `rev:path` revisions and printed protected files in diff and show output.
- [x] Reproduced and fixed after independent review: committed files under configured
      ignored directories (`build`, `node_modules`) appeared in `git.show`, and
      `git.status` listed tracked `.env` changes. Git output is now filtered by
      pathspecs derived from the effective workspace policy; unrepresentable ignore
      names fail closed.
- [x] Reproduced and fixed after a second independent review: repository-controlled
      Git configuration executed programs through the read-only tools. Reproduced with
      marker-file hooks: `core.fsmonitor` (`git.status`, `git.diff`, and in a nested
      repository), `diff.<driver>.textconv` (`git.diff`, `git.show`), and clean filters
      selected by `.gitattributes` or `.git/info/attributes` (`git.diff`). Mitigated at
      the shared subprocess boundary (fixed overrides plus per-repository key
      overrides, `--no-textconv`, `--ignore-submodules=all`). Not reproduced, disabled
      as defense in depth: hooks, `log.showSignature` with `gpg.program`.
- [x] Reviewed with existing passing tests, no defect found: policy precedence
      (deny over approval over allow), custom allow cannot bypass permissions or
      approval, sandbox-unavailable fails closed, MCP output cannot expand the toolset,
      concurrent tenant and user isolation. Added a test in which a scripted model
      receives injected file content as a tool result, then requests a write without
      the `workspace.write` permission; the policy denies it at execution. The model is
      scripted, so this verifies the permission boundary, not model resistance to
      injection.
- Rejected: model-supplied idempotency keys (no adapter populates
  `ToolCall.idempotency_key`); cross-tenant job listing (`JobContext` requires tenant
  and user).
- Not verified or not addressed: 8.3 short names, junctions, and hard links on
  Windows; time-of-check to time-of-use races on symlinks; secret values embedded in
  ordinary files (redaction of model-facing tool output); MCP descriptor text reaching
  the model; real sandbox isolation (none ships).
- The Git execution mitigation is a denylist of known program-launching keys, not a
  sandbox; an unlisted key or a future Git feature could start a program.
- Tested guarantees are limited to the path-based policy above. Git history is
  filtered by current path only (a renamed protected file appears under its ordinary
  name), `git.log` shows commit metadata, and secrets inside ordinary files are not
  detected. Hosts must still isolate untrusted local processes.
- [x] Merged as pull request 4 (squash commit `4c5ff65`) after exact-head CI passed
      (10 checks).

### M5: Runtime and multi-agent reliability (completed)

Each item was reproduced with a failing test before its fix (see ADR 0011).

- [x] Cancellation: `Agent.run` ended `running`, `TaskScheduler.run()` left the worker
      running and its resource locked, `WorkflowController` left the durable run
      `running`, and `JobManager.wait()` swallowed its caller's cancellation. All
      observed and fixed; cancelled is distinct from failed (`run.cancelled`, updated
      JSON Schema, TypeScript, UI state, and a Python/schema/TypeScript consistency
      test). Interrupted durable side effects become `UNCERTAIN`; a workflow with an
      executing or uncertain action is `BLOCKED`, not `CANCELLED`.
- [x] Idempotency: concurrent identical side-effecting calls ran twice and a key could
      be reused with different arguments. Both observed and fixed on the in-memory and
      durable paths; uncertain outcomes are never replayed (tested across restart).
      Autonomous side-effecting tools now require a durable action-intent store.
- [x] Tool and provider errors: opt-in sanitized, bounded `tool_errors="return"`;
      fatal classes unchanged; malformed OpenAI arguments raise `ModelResponseError`;
      transient failures are classified by default in `FailoverModelProvider` (locality
      and state affinity unchanged); OpenAI `timeout` and `max_retries`, Ollama
      `timeout`; contract tests built on the real `openai` response models and
      exception classes.
- [x] Recovery: unobserved `CodingKit` background exceptions observed and fixed; loss
      of a still-running discarded task was suspected but not reproducible, so strong
      references were added as hardening. Stale `RUNNING` runs are handled with a
      heartbeat ownership lease and `owner_lost` blocking in recovery; runs without a
      lease are never guessed at and nothing resumes automatically.
- [x] Multi-agent: deterministic integration tests over `TeamRuntime`,
      `SubagentDelegator`, `JobManager`, and the scheduler (delegation chain, failure
      propagation, cancellation cascade, capability and depth bounds, serialized
      writers). They exposed one more observed defect, fixed: jobs sharing a resource
      key failed with `SchedulerStalledError` instead of waiting.
- Coverage of the reliability paths (branch coverage, local run of the full suite):
  `agent.py` 96%, `tools/execution.py` 92%, `models/openai.py` 94%,
  `models/failover.py` 90%, `models/errors.py` 94%, `runtime/durability.py` 94%,
  `runtime/agent_runtime.py` 90%, `workflow/controller.py` 83%, `tasks/scheduler.py`
  81%, `jobs/manager.py` 77%, `kits/coding/facade.py` 73%.
- Not done or not verified: run-level wall-clock or token bound; first-party
  `CodingRunControl` and `CodingTeamLauncher` presets (M6); a Gemini timeout option and
  Gemini or Ollama transient classification against real SDK exceptions (the classifier
  is covered with stand-in error shapes only); real PostgreSQL, Redis, and multi-process
  verification of leases and single-flight; Windows 3.12 and 3.13 beyond CI.
- Review corrections (reproduced with failing tests first):
  - Legacy action intents: real records produced by the pre-M5 executor (fixture
    `tests/fixtures/legacy_action_intents.json`) showed no duplicate execution, because
    the store's owner, run, and key uniqueness blocked it, but succeeded replays asked
    for approval again, different call ids and changed arguments raised raw
    `ValueError`, and a pending legacy approval was stranded. Legacy intents are now
    adopted by key and keep their receipt, uncertainty, and approval.
  - Uncertain-action lookup: cancellation checked only the first 1,000 intents, so a run
    with an in-flight action beyond them was recorded as `CANCELLED`. It now uses a
    complete run-scoped query and blocks when a lookup is incomplete or fails.
  - Run-state races: a recovery pass overwrote a state the owner wrote after the
    snapshot, and a stale executor completed a run another owner was executing. Run
    writes are now conditional on status and the owner's fencing token inside the store
    transaction.
- Compatibility: `AgentRun.status` gains `cancelled`; `ToolResult` gains `is_error`;
  `Agent` and `AgentRuntime` gain keyword-only options; `RuntimeRunRecord` gains
  optional `owner_lease_key` and `owner_fencing_token`; `save_runtime_run` gains
  optional conditional arguments; `IdempotencyConflictError` is also a `ValueError`;
  action-intent ids no longer include the arguments checksum (legacy intents are
  adopted, see `docs/persistence.md`); one durability test now expects
  `IdempotencyConflictError` for a changed-argument replay.
- [x] Merged as pull request 5 (squash commit `95537fb`).

### M6.0: Capability, architecture and developer-experience audit (completed)

- [x] Read-only audit of the coding team, developer experience, memory and context, and
      lifecycle at `95537fb`. It changed no repository files. Findings that are
      reproduced are fixed in M6.1; the rest are unverified or planned and are not
      recorded as complete here.

### M6.1: Context correctness and regression hardening (completed)

Each defect was reproduced with a failing test before its fix.

- [x] Recent conversation: `RecentConversationSource` read the first 1,000 stored
      messages and took the end of that window, so "recent" context froze at message
      1,000. It now reads the newest window through the optional
      `ConversationWindowStore.list_latest_messages` capability (in-memory and SQLite
      implement it, filtered by tenant, conversation and user in the query), with a
      bounded forward-paging fallback for custom stores that implement only
      `ConversationStore`. `list_messages` and its `after_id` contract are unchanged.
- [x] Budget priority: items of equal priority and relevance were kept oldest-first, so
      constrained budgets dropped the newest turns. `ContextItem.order` (optional)
      makes the allocator keep the newest first and then list survivors
      chronologically. Items without `order` behave exactly as before; required items,
      priorities and the current input are unaffected.
- [x] Compaction: `ContextCompactionManager` read the oldest `message_limit` messages
      and looked for its cursor there, so it never advanced after the first 1,000
      messages. It now reads forward from the stored cursor with `after_id`, gathers at
      most `message_limit` candidates plus `recent_messages`, and keeps raw history
      untouched. A missing cursor is still rejected (`source_cursor_missing`) when the
      store provides `message_exists`.
- Not done or not verified: a long backlog is summarized one bounded batch per
  compaction; `source_message_ids` on a summary grows with the conversation; stores
  without the window capability read the conversation from its start to find the
  newest window and cannot tell a missing cursor from an empty tail; the in-memory
  long-term memory store and its scaling limits (M6.0) are untouched.
- Deliberately not changed: Coding Team, sandbox, supervisor and lifecycle code.
- [x] Merged as pull request 6 (squash commit `8b74b65`) after all 10 CI jobs passed on
      the exact head.

### M6.2: Secure container execution and evidence pipeline (completed)

- [x] `DockerSandboxRunner` (ADR 0012): digest-pinned image, no pulls, non-root, no
      network by default, dropped capabilities, read-only root, resource/time/output
      limits, cleanup on timeout and cancellation, fail-closed without host fallback.
- [x] `ExecutionSnapshot` / `SnapshotBuilder`: validated private copy under the
      workspace read policy with a content-aware fingerprint; symlinks, special files,
      and over-limit workspaces are rejected; per-run copies are re-verified.
- [x] `CommandEvidenceRecorder` and signed `JsonArtifactVerifier` mode: evidence is
      generated from the runner's result, and failed, timed-out, stale, unsigned, or
      other-owner evidence never passes. Optional unattended approval for test, lint and
      typecheck presets with an isolating runner.
- Review corrections: unittest discovery fixture and probe scripts in the real Docker
      tests (presets still reject metacharacters), host-named `trusted_runners` for
      evidence provenance, owner-only per-run directory, and post-run detection of
      modified snapshot files. A flaky fixed-sleep M5 test was changed to a bounded poll.
- Verified: scripted-engine tests for argv, limits, timeout, cancellation, cleanup and
  fail-closed paths; real-container tests run on Linux CI only (required there).
- Not exercised or supported: macOS, rootless Docker, Podman. Not done: pinning the
  execution snapshot to workflow runs and refreshing the fingerprint between workflow
  cycles (M6.3.1), `CodingRunControl`, a prepared-dependency image workflow, evidence for
  excluded paths.
- [x] Merged as pull request 7 (squash commit `87d913a`) after all 10 CI jobs passed on
      the exact head, including the real Docker tests and example on Linux.

### M6.3.1: Run identity, fresh snapshots and evidence correctness (completed)

- [x] `WorkflowController(fingerprint_provider=...)` evaluates every completion gate against a
      fresh trusted fingerprint, brackets a passing evaluation to detect a concurrent change,
      and blocks when the state is unavailable (ADR 0013). The fixed-fingerprint behavior is
      unchanged without a provider. `WorkflowOutcome.verified_input_fingerprint` reports what
      was verified.
- [x] `SnapshotBuilder.fingerprint()` and `WorkspaceFingerprintProvider`: content-aware, read-only,
      equal to the fingerprint of a snapshot of the same bytes.
- [x] `trusted_task_context` (durable-run-checked worker context) and `TrustedEvidencePipeline`
      (one artifact manager and signer for recorder and verifiers).
- [x] A real-container reader-task cleanup defect in the M6.2 timeout path (an exception left
      unretrieved) was reproduced by a failing test and fixed.
- Verified: workflow-level A -> B -> C regressions with real snapshots and signed evidence
  (deterministic runner), the same A -> B -> C flow through real containers, and two Docker
  examples. The real Docker tests also ran locally on Windows 11 with Docker Desktop 28.3.0
  (Linux engine, WSL2), a single development machine, in addition to Linux CI.
- Not done: running a model agent as a worker, supervisor and delegation tools, reviewer
  approvals and milestone acceptance (M6.3.2 to M6.3.4); atomic verification of the workspace.
- [x] Merged as pull request 8 (squash commit `79c31ef`) after all 10 CI jobs passed on the
      exact head, including the real Docker tests and examples on Linux.

### M6.3.2: AgentRuntime worker integration and team context foundation (closed)

- [x] ADR 0014: Private Agent Context + Shared Durable Project State + Bounded Task Handoffs +
      Lead Coordination View, with project, team, agent and lead scopes and the line between
      authoritative records and model-authored text.
- [x] `AgentRuntimeWorker`: a configured `AgentRuntime` as a `Worker` through the existing
      scheduler; host-owned permissions, tools and role; trusted identity from the durable run;
      bounded handoff-shaped result with `verified: false`; cooperative checkpoints and progress.
- [x] `WorkspaceBinding` in the durable run record (optional, write-once, immutable in the store),
      enforced by `WorkflowController(workspace_binding=...)` and by coding workers.
- [x] `AuthorizedCodingWorkspace`: coding workers that can produce evidence are refused unless the
      fingerprint provider, catalog, runner, recorder, signed verifiers and controller agree.
- [x] `TaskContextPackage`, `TaskContextAssembler`, `TaskHandoff`, `private_conversation_id`:
      bounded context through the existing `ContextBuilder`; private per-task conversations;
      handoffs from completed dependency tasks; references verified in the run's owner scope.
- Verified: deterministic AgentRuntime-worker tests (scripted model, not an external LLM), and a
  real-container workflow where an AgentRuntime worker runs tests in Docker and the gate rejects a
  false model claim, plus a demonstrator example.
- Not done: Lead coordination view and delegation tools (M6.3.3), reviewer approval and team
  acceptance (M6.3.4), isolated parallel writers, worktree merging, real external LLM verification,
  atomic workspace verification.
- [x] Merged as pull request 9 (squash commit `1950851`) after all 10 CI jobs passed on the exact
      head. The closing security review corrected four trust-boundary findings, each with a
      regression test: handoff evidence is authenticated with the existing `EvidenceSigner`
      instead of trusting a label; evidence-producing workers require a controller; the binding
      gained a host-assigned `identity_digest`; artifact handoffs are same-run scoped.
- Still open after closure: a real external model has not yet run through this stack (see the
  live provider validation checkpoint below).

### Live provider validation (opt-in, manual)

- [x] `scripts/live_validation.py`: a small harness that runs a real OpenAI or Ollama model as
      Tester through `AgentRuntimeWorker`, `TaskScheduler`, the Docker sandbox, signed evidence
      and the completion gate, with request, step, tool-call, token, time and budget limits,
      sanitized provider diagnostics, separate usage and billing accounting, and a redacted
      report. Nothing external runs without `--live`; CI exercises it only with scripted fakes.
      See `docs/live-validation.md`.
- [x] Strict tool schema fix: `serialize_openai_tool` chooses `strict` per tool and sends a tool
      with a free-form mapping or an unsupported construct as `strict: false` with its original
      schema (Coding Kit command tools). Host-side validation is unchanged.
- Verified manually, not by CI: with `gpt-5.4-mini` one `pass` run (exit code 0, authenticated
  signed evidence, gate completed) and one `negative` run (exit code 1, authenticated evidence,
  gate blocked with `quality_blocked`), both with worker `verified: false`, about $0.0023 each.
  These are single smoke runs on one machine, not reliability evidence.
- Later than the M6.3.2 closure above: M6.3.2's "real external LLM verification" gap was
  closed only by these manual runs; the closure statement itself is historical and unchanged.
- Not done: any Ollama model passing (`qwen2.5-coder:7b` wrote the tool call as text,
  `qwen3:4b` hit the output cap while reasoning); a live confident-false-claim test; repeated or
  multi-model runs; automatic paid tests (deliberately never run in CI).

### M6.3.3: Lead coordination view and delegation tools (in progress)

- [x] PR #11 (squash commit `3f2d967`, merged): workflow worker actions are owned by the durable
      run, a restarted run with an unresolved action of its own is blocked, and the legacy
      inventory API exists. This is the prerequisite for the Lead work below.
- [x] M6.3.3-A (pull request 12, squash commit `276e814`, merged after an independent review and
      its R1 corrections; all 10 CI jobs passed on the exact head and the post-merge run on
      `main` succeeded): ADR 0015 (Proposed when merged; accepted since, see below), the optional
      `TaskRunQuery` capability (owner- and run-scoped, keyset-paged,
      SQL-filtered; SQLite ownership columns verified against `runtime_runs`, which establishes
      membership of a claim in an existing run of the owner and not who wrote the task, with a
      one-time atomic backfill that leaves unverifiable historical rows unavailable), and the
      read-only `LeadViewAssembler`/`LeadView`. Verified by deterministic tests including a real
      workflow integration test and mutation checks of the key protections.
- [x] M6.3.3-A independent review R1 corrections (merged within pull request 12): the Lead view is
      bounded and rejects contract-violating `TaskRunQuery` pages instead of looping, storage
      failures surface as stable `LeadViewError` codes or `unavailable_sources` with sanitized
      logs, reference availability is explicit (`checked` versus `unavailable`, failed versus
      unchecked claims), and the trusted-write boundary of `TaskStore` is documented accurately.
- [x] ADR 0015 accepted by the operator on 2026-11-09 with amendments A1 to A4: the central
      `PolicyEngine` is authoritative and only an explicit allow admits; preliminary admission is
      point-in-time and not a reservation, and `WorkflowCompiler` alone is not a final
      authorization; a delegation-driven revision gets its own reason and the child depth is
      derived from the trusted parent; job and workflow delegation share admission checks and no
      execution path.
- [x] M6.3.3-B shared delegation admission (draft pull request, not merged, pending independent
      review): `DelegationAdmission` in `kaiwen_agent.policy`, `SubagentDelegator` rebuilt on it
      with its public behavior kept (a raising policy and malformed facts now fail closed with a
      sanitized message; opt-in `resource_allowlist` for the unrestricted job-path resource
      keys), `WorkflowDelegationAdmission` for workflow proposals, and the Lead view's bounded
      task scan extracted into `scan_run_tasks`. Admission only: nothing is submitted, created,
      revised or run. Verified by deterministic tests (including honest characterizations of the
      point-in-time and per-instance-lock limits) and mutation checks of the key protections.
- Not done: the cycle-boundary integration, the explicit revision reason and the final
  authorization with fresh policy and limit checks (PR-C1), model-facing proposal tools and
  durable idempotent proposals (PR-C2), bounded cross-cycle handoff over existing stores
  (PR-C3), reviewer approval and team acceptance (M6.3.4). There is no atomic reservation of
  delegation quotas: the job path serializes within one delegator instance only, and the
  workflow admission is preliminary. The Lead view and workflow admission need a store with
  `TaskRunQuery`; custom and in-memory task stores do not provide it yet.

### M7 design items (not started)

- Lifecycle failure recovery: a failed cycle leaves `LifecycleRuntime` in `failed`, and
  `run_next_due` then raises until `start()` is called. A recovery policy needs a
  design (retry, degrade, operator action) before any automatic restart is added.

### Open risks from the initial audit

These come from a read-only code review on 2026-11-07. Items are reproduced by a
failing test before a fix is attempted; those not yet reproduced remain unverified.

- Filesystem and Git read policy bypasses: reproduced and fixed in M4.
- `CancelledError` handling in `Agent` and `TaskScheduler`: reproduced and fixed in M5.
- Failover reachability with shipped adapters: fixed in M5 by default transient
  classification; Gemini and Ollama are covered with stand-in error shapes only.
- Non-atomic idempotency: reproduced and fixed in M5.
- Release verification gaps: addressed in M3 (shared verification workflow).
- Still open: no run-level wall-clock or token bound; no first-party isolated command
  runner; the M3 owner configuration items above.

Publishing, deployment, and GitHub pushes remain separate explicit release steps.
