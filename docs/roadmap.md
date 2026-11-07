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

## Repository milestones (M1-M5)

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

### M2: Root-level Python restructuring (in review)

- [x] `backend/src`, `backend/tests`, and `backend/pyproject.toml` moved to the
      repository root with Git-aware renames; `backend/README.md` consolidated into
      the root README (links are absolute so the PyPI long description renders);
      the `backend/` directory removed.
- [x] CI and release workflows, `AGENTS.md`, `docs/releasing.md`, and example
      READMEs updated; `tests/test_protocol_contract.py` fixture path adjusted.
- [x] sdist constrained to `src/kaiwen_agent`, `tests`, `README.md`, `LICENSE`, and
      `pyproject.toml`. Distribution name and version are unchanged (`0.7.0a1`).
- [~] Not complete until the pull request is reviewed, exact-head CI passes, and the
      owner merges it.
- Verified locally on 2026-11-07 (Python 3.13.5, Node 22.16.0): clean editable
  install; `ruff format --check` and `ruff check` on `src tests examples`; mypy on
  `src/kaiwen_agent` and the 13 CI-typed examples; pytest `282 passed, 1 skipped`,
  identical to the pre-move baseline; the 10 CI-executed examples; wheel and sdist
  builds; `twine check`; `npm ci`, build, protocol check, tests, typecheck, and
  `pack:check`.
- Wheel comparison against a pre-move build: the package file list and file contents
  are identical. The only differences are the long description in `METADATA` and an
  added `licenses/LICENSE` entry, because `LICENSE` now sits next to `pyproject.toml`.
- Not verified locally: Python 3.11 and 3.12, Linux, and the release workflow
  (`workflow_dispatch` only, not exercised).

### M3: CI/CD and packaging hardening (planned)

- [ ] Python version and operating-system matrix matching declared support; build,
      `twine check`, and `npm pack` checks on pull requests; release verification
      aligned with CI; publish the verified artifact; version-consistency check;
      release restricted to the default branch; least-privilege workflow
      permissions; pinned actions.
- [!] Branch protection and required reviewers on the `npm` and `pypi` environments
      are remote repository settings and require the owner. A read-only query on
      2026-11-07 reported no protection on `main`, no rulesets, and no environment
      protection rules.
- [ ] Reconcile repository URLs (`kaiwen-agent-platform` in package manifests versus
      `kaiwen-agent` as the current remote) after confirming trusted-publisher
      configuration on PyPI and npm.

### M4: Security and behavioural validation (planned)

- [ ] Add negative tests first, then fix confirmed defects.
- [ ] Real-contract tests for the OpenAI adapter using recorded SDK response shapes.
- [ ] Coverage measurement for policy, approval, and durability paths.

### M5: Runtime and multi-agent reliability (planned)

- [ ] Explicit cancellation handling and terminal-state persistence in `Agent`,
      `TaskScheduler`, and `JobManager`.
- [ ] Decide the policy for tool and malformed-model-output errors during a run.
- [ ] Provider error classification so failover is reachable with shipped adapters.
- [ ] Run-level wall-clock or budget bound.
- [ ] Durable action store requirement for side-effecting tools.

### Open risks from the initial audit

These come from a read-only code review on 2026-11-07. None has been reproduced by
execution; each needs a failing test before a fix is attempted.

- `WorkspaceFilesystem.read_text` and `write_text` appear not to apply the host
  ignore and sensitive-path policy that `list_files`, `stat`, and `read_lines`
  apply; `workspace.read_text` and `workspace.apply_patch` use them. Targeted by M4.
- `Agent.run` and `TaskScheduler` handle `Exception` but not `CancelledError`,
  which may leave run or task state non-terminal. Targeted by M5.
- No first-party shipped provider adapter raises `RetryableModelProviderError`, so
  initial-call failover depends on host-side classification.
- The default idempotency store is in-memory and its lookup and write are not atomic.
- Release verification omits four example runs that CI performs, and build checks
  run only during release.

Publishing, deployment, and GitHub pushes remain separate explicit release steps.
