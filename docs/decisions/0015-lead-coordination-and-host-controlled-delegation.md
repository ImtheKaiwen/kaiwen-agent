# ADR 0015: Lead coordination view and host-controlled delegation

- Status: Proposed (pending operator review; not accepted)
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
- Absence is explicit. A source the assembler was not given (artifacts, approvals) is
  reported as unavailable, never as empty. A truncated listing says so.
- No raw private conversations or transcripts, no raw exception text (`AgentTask.error` is
  never exposed; only a pattern-checked error code), no credentials, no permissions.

### 3. Run-scoped task query

The projection needs an owner- and run-scoped task read. It is an optional store capability,
`TaskRunQuery.list_run_tasks(tenant_id, user_id, runtime_run_id, limit, after_id)`, with
keyset pagination, a hard page maximum, and database-level filtering. The base `TaskStore`
protocol is unchanged, so custom stores keep working; a consumer that needs the capability
fails closed when it is missing.

Ownership of a stored task is an attested fact, not a copy of arbitrary metadata. The SQLite
adapter fills the owner and run columns only when the task names a durable run that exists
for the same tenant, user and session and its trace id differs from the run id, checked
against `runtime_runs` at write time. Rows written earlier are backfilled once with the same
check; rows that cannot be verified keep NULL columns and are unavailable to the query, never
assigned guessed ownership.

### 4. Host-controlled delegation admission (future, not implemented here)

Delegation is a request, never an execution. A Lead may later propose tasks; the host admits
them. This ADR fixes the shape so PR-A does not foreclose it:

- A proposal is a durable, owner- and run-scoped record with a host-derived idempotency key,
  so a retried proposal cannot admit work twice. Proposal content is untrusted input.
- Admission is a host decision: plan validation by the existing `WorkflowCompiler` (worker
  allowlist, capabilities, resource keys, size limits), workspace binding consistency, and
  limits on count, depth and fan-out. Permissions come from the host's worker
  configuration, never from the proposal.
- Admission happens only at an explicit cycle boundary of the existing `WorkflowController`,
  as a plan revision through the existing planner seam. Tasks are not injected into a
  running graph.
- No fake Completion Gate failure: a delegation request must not be modelled by making the
  gate fail to force another cycle. The gate reports requirement results only.
- Delegation depth and the existing `delegation_depth` field bound recursion.

### 5. Private context and bounded handoffs

Agent context stays private (ADR 0014). The Lead sees task state and bounded handoffs from
the existing assembler rules, not transcripts. Cross-run sharing is not offered.

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

Reviewer approval, team acceptance, isolated parallel writers, worktree merging and
cross-cycle handoff persistence are not part of M6.3.3 and are not designed here.

## Staging

- PR-A (this change set): this ADR, `TaskRunQuery` with the SQLite implementation and
  migration, and the read-only `LeadViewAssembler`. No delegation code.
- Later work packages implement the proposal record and admission only after this ADR is
  accepted. Anything in section 4 may change in review.

## Consequences

- One schema change in the existing SQLite database: three nullable columns on `tasks` and
  one index. Rollback is safe: older code ignores the columns.
- Historical tasks that cannot be tied to a verified run stay invisible to the Lead view.
  That is deliberate; an operator can re-save such a task after the run exists to attest it.
- Custom and in-memory task stores have no Lead view until they implement the capability.
- The view is a snapshot of independently read stores, not a transaction; counts can be
  slightly stale, and it says what it could not read.
