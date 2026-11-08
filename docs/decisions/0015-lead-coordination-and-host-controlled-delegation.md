# ADR 0015: Lead coordination view and host-controlled delegation

- Status: Accepted (operator decision of 2026-11-09, with the amendments A1 to A4 incorporated
  in section 4). The acceptance covers the architecture and the M6.3.3-B implementation, not a
  merge, release or deployment of any pull request.
- Builds on: ADR 0011, ADR 0013, ADR 0014. It does not supersede ADR 0014.

## Context

ADR 0014 defined four scopes (project, team, agent, lead) and said the Lead scope is "a bounded
operational projection of team scope, not a merged transcript", with its coordinator left to
M6.3.3. A Lead agent that coordinates a team needs two things the platform does not have yet:
a trustworthy read of the team's state, and a way to ask for more work without gaining the
ability to run it.

The audit of the code at baseline `3f2d967` found what already owns each concern:

- `TaskScheduler` owns dependencies, resource keys, concurrency, retries, timeouts and
  cancellation. `WorkflowController` owns cycles, repair, the completion gate, leases,
  fencing and cancellation recording. There is one orchestration engine and it stays one.
- `WorkflowCompiler` is the only writer of task identity and `metadata` (`runtime_run_id`,
  cycle, plan version, logical task id). Plan-authored data lives in `task.input`.
- `DurableRunManager` holds the run record: owner, session, workspace binding, status.
- `TaskStore` has `save_task`, `get_task` and `list_tasks(status=)`. The SQLite `tasks`
  table has no owner or run columns, so there is no owner-scoped way to read the tasks of
  one run; `list_tasks` is a global load.
- `TaskContextAssembler` already decides which artifact and evidence references of a run
  are acceptable (owner scope, same run, HMAC-authenticated evidence). The completion gate
  remains the only judge of requirement satisfaction (ADR 0013).
- Action intents and approvals are owned by the durable run (PR #11) and have owner-scoped
  retrieval, per run for intents and per intent for approvals.

The audit of the delegation code at baseline `276e814` (for section 4) found:

- `SubagentDelegator` is the only model-facing delegation boundary. It checks capabilities
  against the caller and the parent job, counts jobs in the `JobStore`, evaluates the central
  `PolicyEngine` (`agent.delegate`, `DelegationPolicy`) and only then calls
  `JobManager.submit_delegated`. REQUIRE_APPROVAL, REVIEW and MODIFY already raise a typed
  approval-required error; DENY raises a denial. It serializes count, policy and submit with a
  per-instance `asyncio.Lock`.
- `PolicyEngine` returns the most restrictive decision (DENY, REQUIRE_APPROVAL, REVIEW,
  MODIFY, ALLOW) and denies by default when no rule applies. A policy that raises propagates
  the raw exception.
- `WorkflowCompiler` validates structure, workers, host-wide capabilities, resource keys and
  size limits. It does not call the `PolicyEngine`, does not know live delegation quotas and
  does not set `delegation_depth` (compiled tasks are depth 0).
- The job path does not allowlist `resource_keys`: `SubagentDelegator` does not check them and
  `JobManager._submit` stores them on the job. The workflow path does, through
  `WorkflowCompiler.resource_allowlist`.
- `WorkflowRevisionReason` has only `EXECUTION_FAILED` and `QUALITY_BLOCKED`, and the
  revision planner is consulted only for those.

## Decision

### 1. Ownership boundaries do not change

The Lead is an ordinary agent worker of the existing workflow. It does not own the run, the
scheduler, leases, approvals, workspace binding or the completion gate. Nothing in this ADR
adds a second scheduler, a job engine, a new run state machine or a new database.

### 2. Read-only Lead coordination projection (M6.3.3-A, specified and built first)

The Lead reads a `LeadView`: a bounded, typed projection of one durable run assembled from
the existing stores by `LeadViewAssembler`. It contains host-derived facts (run id and status,
workspace binding id, task ids, logical ids, statuses, dependencies, attempts, progress,
blocker categories, authenticated evidence references, pending approval references) and a
separate group of untrusted, model-authored text (bounded summaries). Rules:

- Read-only: construction performs no write, no tool call, no task creation, no approval
  resolution and no action-intent mutation.
- Authority labels follow provenance. A field is presented as a host fact only if a host
  component wrote it. A signed evidence reference is authentic, not proof that a requirement
  passed; the view never reports a requirement result, and the completion gate stays
  authoritative.
- Absence is explicit. A source the assembler was not given or could not read (artifacts,
  evidence signer, approvals) is reported as unavailable with a stable code, never as empty.
  Per task, references are `checked` (an empty list is then a real result) or `unavailable`
  (empty says nothing); claims that failed verification and claims that were never evaluated
  are counted separately. A truncated listing says so.
- Bounded and fail-closed. The task listing is bounded by `max_tasks` however a custom
  `TaskRunQuery` behaves: a page that violates the contract (no progress, a cursor that does
  not advance, repeated or out-of-order tasks, more rows than requested, malformed fields) is
  rejected rather than looped on. Storage failures surface as a `LeadViewError` with a stable
  code, never as a raw exception, and logs carry only the code and the exception type.
- No raw private conversations or transcripts, no raw exception text (`AgentTask.error` is
  never exposed; only a pattern-checked error code), no credentials, no permissions.

### 3. Run-scoped task query

The projection needs an owner- and run-scoped task read. It is an optional store capability,
`TaskRunQuery.list_run_tasks(tenant_id, user_id, runtime_run_id, limit, after_id)`, with
keyset pagination, a hard page maximum, and database-level filtering. The base `TaskStore`
protocol is unchanged, so custom stores keep working; a consumer that needs the capability
fails closed when it is missing.

The SQLite adapter fills the owner and run columns only when the task names a durable run
that exists for the same tenant, user and session and its trace id differs from the run id,
checked against `runtime_runs` at write time. Rows written earlier are backfilled once with
the same check; rows that cannot be verified keep NULL columns and are unavailable to the
query, never assigned guessed ownership.

What this check establishes, precisely: **membership**. The claimed run exists and belongs to
the same owner and session. It does not establish **origin**: it does not show that
`WorkflowCompiler` or `TaskScheduler` wrote the task, or that the run's controller created it.
A component that can call `save_task` and names an existing run of the same owner is accepted.

Trusted-write boundary. `TaskStore` is a host-controlled, in-process interface, and every
component that can call `save_task` is trusted to record truthful ownership. Audit of the
code at the baseline: `TaskScheduler` is the only caller of `save_task`; `WorkflowCompiler` is
the only writer of `runtime_run_id` into task metadata; the other places that build tasks
(`JobAdapter`, the realtime task bridge, `SkillRegistry`) set fixed metadata keys that never
include a run id, and model-authored data reaches `task.input` only, which is never consulted
for ownership. No untrusted entry point can therefore write `runtime_run_id`. This is pinned
by a test (`test_only_the_workflow_compiler_writes_a_run_id_into_task_metadata`) so a new
writer is a deliberate, reviewed change. No cryptographic task signing is added: the threat it
would address (a trusted host component forging ownership) is outside the boundary this ADR
assumes, and nothing found requires it. A deployment that exposes `save_task` to less-trusted
code must add its own authorization in front of it.

### 4. Host-controlled delegation admission

Delegation is a request, never an execution. A Lead proposes; the host admits. A proposal is
untrusted input, and permissions come from the host's worker configuration, never from the
proposal. A proposal is a durable, owner- and run-scoped record with a host-derived idempotency
key, so a retried proposal cannot admit work twice (PR-C2). Admission happens only at an
explicit cycle boundary of the existing `WorkflowController`; tasks are not injected into a
running graph. The amendments A1 to A4 of the operator decision are part of this section.

**4.1 Central policy authority (A1).** Admission reuses the central `PolicyEngine` and its
`agent.delegate` action (`DelegationPolicy` and any other registered rule). It adds no second
policy engine. Only an explicit ALLOW admits. DENY fails closed. REQUIRE_APPROVAL, REVIEW and
MODIFY never authorize and never modify what runs: they produce a typed approval-required
outcome, as `SubagentDelegator` does with `DelegationApprovalRequiredError`, and a MODIFY
decision's modifications are not applied. Until an approval workflow exists for delegation
(M6.3.4), approval-required means not admitted. A policy lookup or evaluation error fails
closed with a stable, sanitized outcome that carries no exception text. The policy context is
assembled by the host from trusted records; model-authored fields never set identity,
permissions, capabilities, depth, counters or authorization metadata.

**4.2 Preliminary versus final admission (A2).** Two different things must not be confused:

1. A preliminary admission evaluation is a point-in-time answer to "would this be admitted
   now". It is not a reservation: it holds no counter and no lock, it does not guarantee a
   later submission stays authorized, and concurrent preliminary evaluations can all be
   admitted.
2. An authoritative final admission happens at the point where work becomes executable. Only
   it authorizes work, and it must run fresh policy and limit checks.

`SubagentDelegator` serializes counting, policy evaluation and submission with a per-instance
lock. That keeps the parallel limit within one delegator instance. It is not cross-process or
cross-instance atomicity: two delegators over one `JobManager` can both pass the same counters.
This ADR claims nothing more, and M6.3.3-B adds no distributed reservation system.
`WorkflowCompiler` validates plan structure, workers, host-wide capabilities, resource keys and
size limits. It does not evaluate the `PolicyEngine` and does not know live delegation quotas,
so running it alone is not sufficient authorization. PR-C1 must provide the final
host-controlled authorization boundary at the cycle boundary, with fresh policy and limit
checks, before a delegated task is compiled into a graph.

**4.3 Revision reason and host-derived depth (A3).** A delegation-driven workflow revision has
its own explicit `WorkflowRevisionReason`, added with the controller integration in PR-C1. It is
never reported as `EXECUTION_FAILED` or `QUALITY_BLOCKED`, and no fake Completion Gate failure
is used: the gate reports requirement results only. Delegation depth is derived from the trusted
parent task record and incremented by the host (child depth is parent depth plus one). Depth,
owner, capabilities and parent identity from a proposal are never authoritative. Parent depth,
which policies compare against `max_depth`, and child depth, which is recorded on the new task,
are distinct values. The existing per-agent parallel limit and per-run limit are preserved.

**4.4 Workflow and job delegation are different paths (A4).** `SubagentDelegator` submits jobs
through `JobManager`. The future Lead path revises a durable workflow plan through
`WorkflowController` and `TaskScheduler`. They share policy evaluation and admission checks and
nothing else: there is no shared submission or execution mechanism, and no second orchestration
engine. Responsibilities:

- Reusable admission (shared): `PolicyEngine` evaluation, capability subset checks, an optional
  resource-key allowlist, fail-closed validation of host-supplied facts, and the mapping of a
  policy decision to an outcome.
- Job-specific: parent job ownership and assigned worker, caller depth equal to parent depth,
  counting jobs in the `JobStore`, the `JobManager` allowlist, depth and parent checks, and the
  submission itself.
- Workflow-specific: the trusted run (owner, session, running), the workspace binding, the
  trusted parent task and its depth, counting delegated tasks of the run, plan validity by
  `WorkflowCompiler`, and the proposal size bound.
- Final execution-time authorization: PR-C1.
- Execution: `TaskScheduler` and `JobManager`, unchanged.

Resource keys: the job path does not allowlist `resource_keys` (see the audit above). They are
lock names shared across jobs and tenants, so an unrestricted key lets a delegated job contend
for any key. That is an availability concern, not a data-access one. M6.3.3-B adds an opt-in
`SubagentDelegator(resource_allowlist=...)` and keeps the default unrestricted so that existing
behavior does not change; hosts that allow model delegation should configure it. The workflow
admission path requires the compiler's allowlist and denies any other key.

### 5. Private context and bounded handoffs

Agent context stays private (ADR 0014). The Lead sees task state and bounded handoffs from
the existing assembler rules, not transcripts. Cross-run sharing is not offered. Bounded
cross-cycle handoff over the existing stores (task outputs, artifacts and the
`TaskContextAssembler` rules) may be part of PR-C3; a new cross-run sharing or persistence
system is not authorized.

### 6. Run and workspace isolation

Every read is scoped by tenant, user and durable run before rows are materialized. A task
whose recorded owner or run disagrees with the requested run is dropped and counted. A run
whose workspace binding differs from the binding the host expects is refused. No cross-run,
cross-tenant or cross-user data is returned.

### 7. Security and recovery invariants

- Trusted facts are host-written; model-authored text is data and grants nothing.
- Idempotency and ownership use the durable run (PR #11); a proposal never reuses a tool
  call id as identity.
- Recovery semantics are unchanged: uncertain actions block a run until an operator
  reconciles them, and the view reports that blocker category without resolving it.
- Fencing and leases are unchanged; the view takes no lease and never writes.

### 8. M6.3.4 exclusions

Reviewer approval, team acceptance, isolated parallel writers, worktree merging and a new
cross-run sharing or persistence system are not part of M6.3.3 and are not designed here.

## Staging

- PR-A (merged, `276e814`): this ADR as Proposed, `TaskRunQuery` with the SQLite
  implementation and migration, and the read-only `LeadViewAssembler`.
- PR-B (M6.3.3-B): this ADR accepted with the amendments, the shared `DelegationAdmission`
  contract, `SubagentDelegator` rebuilt on it with unchanged behavior, and the workflow
  admission adapter. Admission only: no job submission, task creation, proposal storage,
  plan revision or scheduler call.
- PR-C1: controller integration at the cycle boundary, the explicit revision reason, host
  derivation of delegated task depth, and the final authorization with fresh policy and limit
  checks.
- PR-C2: model-facing proposal tools and durable, idempotent proposal storage.
- PR-C3: bounded cross-cycle handoff over the existing stores.
- M6.3.4: reviewer approval and team acceptance, including any approval workflow for
  delegation decisions that need one.

## Consequences

- One schema change in the existing SQLite database: three nullable columns on `tasks` and
  one index. Rollback is safe: older code ignores the columns.
- Historical tasks that cannot be tied to a verified run stay invisible to the Lead view.
  That is deliberate; a host component can re-save such a task after the run exists, which
  re-verifies its claim (an operator action inside the trusted-write boundary above).
- Custom and in-memory task stores have no Lead view until they implement the capability.
- The view is a snapshot of independently read stores, not a transaction; counts can be
  slightly stale, and it says what it could not read.
