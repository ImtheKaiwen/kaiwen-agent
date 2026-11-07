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

## SSE

`SSEEventBroker` can persist an event to another sink and fan it out to active
session subscribers. It yields framework-neutral SSE strings, so FastAPI, Starlette,
or another HTTP adapter can wrap the stream without entering the core package.

## Audit redaction

`RedactingAuditStore` recursively removes credential-like keys and common API key or
Bearer token values before forwarding a record. Product applications should still
avoid adding unnecessary personal or secret data to audit details.
