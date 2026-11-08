# Persistence and event delivery

## Ports

The core exposes four independent persistence boundaries:

- `SessionStore` for application session metadata.
- `RunStore` for lifecycle and completion state.
- `EventStore` for ordered replayable runtime events.
- `AuditStore` for security-relevant execution records.

Applications may use one adapter for all four boundaries or compose separate stores.
The core does not require SQLAlchemy, a web framework, or a specific database.

`AgentEvent` can carry trusted `tenant_id` and `user_id` ownership alongside its
session ID. External/user-facing readers should depend on `ScopedEventStore` and call
`list_events_scoped(EventScope(...))`, which requires an exact tenant, user, and
session match. The original `list_events(session_id=...)` method remains available
for backward-compatible trusted internal replay and must not be exposed directly as
an authorization boundary.

Durable workflow composition adds three separate boundaries: `RuntimeRunStore` for
owner-scoped run state and immutable version pins, `ActionIntentStore` for external
side-effect state and receipts, and `DurableApprovalStore` for one-time decisions.
These records are intentionally separate from conversations and the compact legacy
`AgentRun` model.

## SQLite reference adapter

`SQLitePersistence` implements all four boundaries using the Python standard library.
It enables WAL mode, uses a monotonically increasing event sequence, and opens a
short-lived connection for each async operation through `asyncio.to_thread`.

```python
from kaiwen_agent.persistence import SQLitePersistence

persistence = SQLitePersistence("runtime/agent.sqlite3")
agent = Agent(
    name="example",
    model=provider,
    event_sink=persistence,
    run_store=persistence,
)
```

SQLite is the reference and local-development adapter. Multi-instance deployments
should implement the same ports with PostgreSQL or another transactional store.

Existing SQLite event tables gain nullable ownership columns in place. Historical
rows are not assigned guessed owners: they remain readable through the legacy trusted
API but fail closed and are invisible through scoped reads. New agent, task, job,
policy, artifact, objective, watch, trigger, and attention events propagate trusted
owner identity when their source context has it.

### Run-scoped task query and task ownership columns

`SQLitePersistence` implements the optional `TaskRunQuery` capability,
`list_run_tasks(tenant_id, user_id, runtime_run_id, limit, after_id)`. It filters by owner and
run in SQL before any row is materialized, orders by task id, pages with a keyset cursor
(`after_id`, `TaskRunPage.next_after_id`), and rejects a `limit` outside `1..200`. The base
`TaskStore` protocol is unchanged, so custom and in-memory stores keep working; consumers that
need the capability, such as the Lead view, fail closed without it.

The `tasks` table gains nullable `tenant_id`, `user_id` and `runtime_run_id` columns and the
index `idx_tasks_owner_run`. They hold an attested owner, not a copy of task metadata: a task is
attested only if it names a run that exists in `runtime_runs` for the same tenant and user,
the run's session matches, and the trace id differs from the run id. `save_task` re-checks this
on every write, so saving a task again after its run exists attests it; anything else stores
NULL and is invisible to the query (the task stays readable through `get_task`).

On first start after upgrading, one transaction under the write lock adds the columns and
attests existing rows with the same check, in batches. Rows that cannot be attested (malformed
JSON, no run, a run of another owner or session, non-string metadata) keep NULL columns and are
unavailable, never assigned a guessed owner. A crash leaves the old schema and the next start
retries; a concurrent starter waits and finds the work done. Older code ignores the columns,
so rolling back is safe. To make an unavailable historical task visible, re-save it once its
run exists (the scheduler already does this when a task changes).

`SQLitePersistence` also implements the durable workflow stores. The standalone
`SQLiteIdempotencyStore` persists the existing tool receipt contract and may share
the same database file. A durable tool execution supplies both stores explicitly:

```python
from kaiwen_agent.persistence import SQLiteIdempotencyStore, SQLitePersistence
from kaiwen_agent.tools import DurableApprovalGate, ToolExecutor

state = SQLitePersistence("runtime/agent.sqlite3")
executor = ToolExecutor(
    approval_gate=DurableApprovalGate(state),
    action_intent_store=state,
    idempotency_store=SQLiteIdempotencyStore("runtime/agent.sqlite3"),
)
```

Pending approvals and prepared actions survive restart. A process interruption while
the handler may already have produced an external effect is not treated as a normal
retry: `RuntimeRecoveryManager` marks the intent uncertain and blocks the run for
provider reconciliation or human review. Recovery must hold an injected `LeaseStore`;
multi-host deployments must use a distributed implementation and enforce fencing at
the protected write boundary. This is at-least-once execution, not exactly-once.

## Run ownership and recovery

`WorkflowController` accepts an optional `LeaseStore`. While it executes a run it
heartbeats a lease named `runtime-run:<run id>`, records the key on the run, and stops
without writing state if the lease is lost or if another executor already holds it
(`RunOwnershipError`). `RuntimeRecoveryManager.reconcile` blocks `RUNNING` runs whose
recorded lease nobody holds (`owner_lost`). Runs with no recorded lease are left alone
because age alone does not prove the process died, and recovery never resumes a run
automatically. The in-memory lease store protects a single process; use a shared store,
such as the Redis adapter, across nodes.

Run-state writes are conditional. `RuntimeRunStore.save_runtime_run` accepts
`expected_status` and `expected_owner_fence`, and the SQLite store compares them with
the stored run inside the same transaction that writes (`RuntimeRunConflictError`
otherwise). The controller passes its lease's fencing token on every transition, so a
paused or stalled executor whose lease was taken over cannot complete, fail, or block a
run the new owner is executing, and a recovery pass cannot overwrite a state the owner
wrote after recovery listed the runs.

## Upgrading action intents persisted by 0.7.0a1 and earlier

Action-intent ids no longer include the arguments checksum. No migration is required
for the SQLite store: when an intent is not found under its new id, the executor looks
it up by owner, run, and idempotency key (`get_action_intent_by_key`) and continues
with the stored record, so receipts, uncertainty, and pending approvals survive.
Replays of a legacy succeeded action return its receipt, an executing or uncertain one
raises `ActionOutcomeUncertainError`, and the same key with different arguments raises
`IdempotencyConflictError`. Recommended procedure:

1. Stop the old process, then start the upgraded one and run
   `RuntimeRecoveryManager.reconcile` once so intents left `EXECUTING` become
   `UNCERTAIN` and their runs are blocked for review.
2. Review uncertain actions in the owning system before any manual retry.
3. A custom durable store should implement `get_action_intent_by_key` (and, for complete
   cancellation checks, `list_run_action_intents`). Without them no legacy action is
   executed again, but a replay can require approval again or fail with a conflict.

## Workflow worker actions belong to the durable run

A side-effecting tool called by a workflow `AgentRuntimeWorker` runs inside a
per-invocation `AgentRun`, but its action intent and durable approval are owned by the
durable workflow run (`runtime_run_id` is the `runtime_...` id), so
`WorkflowController` cancellation and `RuntimeRecoveryManager` find in-flight actions by the
run they belong to and block that run when an outcome is uncertain. The host passes a
`DurableActionScope` (run id and task id) built by `trusted_action_scope` from a run that
`trusted_task_run` verified; it is never read from model output, tool arguments or context
metadata, and the executor and `AgentRuntime` reject a scope that disagrees with the
execution owner. Within a run, the idempotency identity of an action is the task plus the call
id (or the call's idempotency key), so tasks that reuse a provider-generated call id do not
share an intent or an approval, while a retried task cannot repeat a keyed action. Standalone
`AgentRuntime` and `Agent` executions keep the per-invocation scope unchanged.

Support for the scope is a declared capability, not a signature: a `ToolExecutor` subclass that
overrides `execute` must accept `action_scope`, enforce it (normally by passing it to
`super().execute`) and set `supports_action_scope = True` on that class. `**kwargs` is not
evidence, because an executor could accept the scope and ignore it, attributing a side effect to
the wrong run. `Agent.run` and `ToolResultProcessingExecutor` refuse, before any model call or
tool, a scope for an executor that does not declare support (`TypeError`); a wrapper also
requires its inner executor to. Executors without a scope are unaffected. This is a contract for
cooperating implementations: a malicious executor can declare support it does not honor.

A restarted run (blocked or paused) never starts new work while one of its own actions is still
executing or uncertain: `WorkflowController` re-blocks it with `uncertain_action:<intent id>`,
because a different tool-call id is not proof of a different external operation. The same
happens when the lookup is incomplete or fails. An operator resolves the action explicitly (see
below) and the run can then restart. This check needs `WorkflowController(action_intents=...)`.

The start check counts `EXECUTING` as well as `UNCERTAIN`, and it does not assume that
`RuntimeRecoveryManager.reconcile` ran first (the controller never runs recovery). When a run
starts it is not `RUNNING`, and with a lease store the controller holds the exclusive run lease,
so a live owner cannot have an action in flight for it: an `EXECUTING` action of that run is the
orphan of a dead owner with an unknown outcome. A live owner is excluded by the lease
(`RunOwnershipError`, nothing is touched), and actions of other verified runs never block this
one. Tests that need an in-flight action must create it while the run executes.

Records written by earlier versions for workflow workers carry the per-invocation `run_...`
id as their owning run, and nothing persisted says which durable run they belonged to, so the
owning run is never guessed. Three different guarantees apply to such a record:

1. It is not modified or rebound by this change.
2. That exact record is never replayed: an executing or uncertain action raises
   `ActionOutcomeUncertainError`, and recovery marks an interrupted one uncertain.
3. A semantically identical new action is **not** prevented automatically. A workflow task that
   repeats the same external effect gets a new, differently owned intent and runs.

To make (3) fail closed, run the upgrade with `WorkflowController(action_intents=...,
block_unassociated_actions=True)`. A run of an owner who has an executing or uncertain action
that no known durable run owns is then blocked with `unassociated_action:<intent id>`; no
listing, no store and any lookup error also block.

`RecoveryReport` is not an inventory. `reconcile` only processes actions that are EXECUTING at
that moment, so `unassociated_action_ids` lists the ones that pass found and made uncertain. An
action that was already UNCERTAIN (an earlier startup, an interrupted approval) is invisible
to it, so an empty report does not prove there is nothing to reconcile. The complete list is
`RuntimeRecoveryManager.inventory_unassociated_actions(tenant_id=..., user_id=..., limit=...)`:
read-only, owner-scoped and bounded; it covers every executing or uncertain action of the owner
whose recorded run id is not a durable run of that owner, and never infers an owning run. It
returns `complete=False` when the bounded page may hide more; then its ids are only a lower
bound.

Operator procedure, for every owner (tenant and user) with workflow workers:

1. Stop old processes, run `RuntimeRecoveryManager.reconcile`, then call
   `inventory_unassociated_actions` for each owner.
2. Check each listed action in the owning external system.
3. Record the verified outcome explicitly with
   `transition_action_intent(expected={UNCERTAIN}, status=SUCCEEDED | FAILED | CANCELLED,
   error_code="operator_reconciled")`. Use `expected={EXECUTING}` with `UNCERTAIN` first if
   reconcile has not run. History is never rewritten or deleted.
4. Repeat until the inventory is empty and `complete` is true. Only then may the upgrade run
   without the guard.

The guard cannot tell a legacy workflow action from an unresolved action of a standalone agent
with the same owner, so it may also hold workflows until that one is resolved. That is the
conservative side of the missing provenance.

Why the guard is opt-in. The only situation it protects is an upgrade with unresolved legacy
records; elsewhere it is a no-op that costs one bounded listing per run.
- Fresh installations and existing databases without unresolved legacy records: nothing to
  protect, so the default stays off.
- Upgrades with unresolved legacy records: enable it for the upgrade (the procedure above).
- Custom action-intent stores: it needs `list_action_intents` and fails closed when the store is
  absent, the page is full or the call fails. Default-on would block workflows on stores that
  cannot list completely.
- Standalone agents: their unresolved actions look identical to legacy ones, so default-on would
  block unrelated workflows of the same owner.
Provenance that separates legacy workflow actions from other actions does not exist, so no
narrower default is possible without a breaking change. Hosts that upgrade with unresolved
records must follow the procedure or enable the guard; there is no automatic protection.

## SSE

`SSEEventBroker` can persist an event to another sink and fan it out to active
session subscribers. It yields framework-neutral SSE strings, so FastAPI, Starlette,
or another HTTP adapter can wrap the stream without entering the core package.

## Audit redaction

`RedactingAuditStore` recursively removes credential-like keys and common API key or
Bearer token values before forwarding a record. Product applications should still
avoid adding unnecessary personal or secret data to audit details.
