# ADR 0016: Fenced execution over one SQLite database

- Status: Accepted, scoped (operator decision of 2026-11-09: Option A, transactional SQLite
  ownership and fencing, together with Option C, fail-closed rejection of unsupported
  configurations). The acceptance covers this foundation only. It does not approve a merge, a
  release, Redis plus SQLite delegation, multi-host delegation, or the downstream cycle-boundary
  integration (pull request 14), which stays blocked until this foundation is merged and the
  integration is re-verified on top of it.
- Builds on: ADR 0011 (run ownership), ADR 0015 (delegation). It does not change either.

## Context

`WorkflowController` holds a heartbeat lease per run and compares the lease's fencing token with
the token stored on the run record. The review of pull request 14 reproduced why that is not
enough to exclude a stale owner: the epoch that decides ownership lives in the lease backend, and
the run record only learns of a new epoch when the new owner writes it. Between the two, and
whenever a lease merely expires, a controller that lost the lease can still persist tasks and
start workers, because `TaskStore.save_task`, the scheduler and the action-intent claim carry no
authority and check none. Checking or renewing the lease before a write is a check at one
instant, not a guard on the write.

## Decision

Make the lease authority and the protected records one transactional system and enforce the
epoch inside each protected write's own transaction, for one configuration only.

- Option A. `SQLiteLeaseStore` keeps leases in the same SQLite database file as the tasks, runs
  and action intents, through the same `SQLitePersistence` object. Acquisition and epoch
  allocation, renewal and release are each one `BEGIN IMMEDIATE` transaction. A fenced write
  reads the lease row, the run record and the target in its own transaction and refuses unless
  the epoch is the live holder. There is no window between "the lease changed" and "the store
  knows".
- Option C. Everything that cannot enforce this is refused at construction or before it writes:
  Redis leases, in-memory leases and stores, a lease database that is not the protected
  database, a second adapter on the same path, custom task stores, schedulers not built over the
  fenced task store, and side-effecting tools without a fenced intent store. Nothing downgrades
  silently.
- Not chosen: Redis plus SQLite (publish a durable epoch after acquiring in Redis). It leaves the
  interval between acquisition and publication open by design and cannot detect an expired lease
  with no successor. Multi-host PostgreSQL or Redis delegation is a separate, later milestone.

## Authority model

- Epoch. The fencing generation is per lease key (`runtime-run:<run id>`), starts at 1, only
  increases and survives release and expiry (a release clears the credential, never the row).
- `OwnerEpoch`. Issued only by `SQLiteLeaseStore.issue_owner_epoch`, which re-reads the lease row.
  It carries the database id, lease key, owner id, generation, run, tenant, user, session and the
  lease credential (a random secret, excluded from `repr`). It is not trusted by type: each
  protected write re-validates all of it against the authoritative rows, so a forged, stale or
  foreign epoch is refused (`lease_not_held`, `lease_superseded`, `lease_expired`,
  `database_mismatch`, `binding_mismatch`). It is host state, never read from task fields, tool
  arguments, prompts or model output.
- Propagation. The controller binds the epoch in a `contextvars` scope for the code it runs.
  `FencedTaskStore` and `trusted_action_scope` read it there; a store used outside the scope has
  no epoch and refuses. This is composition, not a security boundary against arbitrary Python in
  a worker, which could call the persistence layer directly.
- Clock. Expiry is judged by `SQLitePersistence.lease_clock` (UTC wall clock, millisecond
  resolution), the single clock shared by the lease store and every fenced write, so they cannot
  disagree. The clock is read inside each protected transaction, after the write lock is held,
  never before waiting for it (a sample taken earlier can be arbitrarily stale when contention
  delays the transaction); tests inject a deterministic clock there and a second connection that
  holds the lock. A clock that steps backwards can extend a lease and one that steps forward can
  end it early; this is why the mode is limited to one local database file (one host, one clock).

## What is enforced, and where

- Fenced task writes: `SQLitePersistence.save_task_fenced(task, epoch)`. Same transaction: lease
  row, run record (tenant, user, session, RUNNING, this epoch published), task ownership, upsert.
  Creation, queueing, RUNNING, progress, completion, retry and cancellation records all go through
  it when the scheduler is built over `FencedTaskStore`.
- Fenced run transitions: `save_runtime_run(..., owner_epoch=...)` for start, complete, block,
  fail and cancel (pause shares the same code path as the others). Starting publishes the epoch and
  needs a strictly higher epoch than any published; every other transition needs the run RUNNING
  under exactly this epoch. A run that was started fenced (`RuntimeRunRecord.fenced`) keeps that
  flag for good: no write can clear it, and only a fenced start can set it. Likewise its published lease
  key and fencing generation are never rewritten by a transition, an operator action, a recovery
  block or a direct store update (`fenced_owner_immutable`); only a fenced start publishes a new,
  strictly higher epoch. While such a run is
  RUNNING, no write without an epoch is accepted (complete, fail, cancel, pause, block, or a
  direct store update). Once it is not RUNNING (blocked, paused), operator transitions keep
  working, because no executor holds authority, but starting it still needs an epoch. Other runs
  and stores are unchanged.
- Recovery authority (narrow, explicit). `RuntimeRecoveryManager` touches a fenced RUNNING run
  only with the run's own lease, acquired as a probe (so the owner's lease is gone) and newer than
  the published epoch, issued as a `recovery_epoch`. With it, a run write can only move the run to
  BLOCKED and an intent write can only move EXECUTING to UNCERTAIN; recovery can never complete,
  fail, pause or restart a run, finish an action or record a receipt. A live owner is left alone.
  Recovering fenced runs needs the same `SQLiteLeaseStore`; with any other lease store the run is
  left untouched (fail closed), and an operator cancel of a RUNNING fenced run must go through the
  controller or a recovery pass.
- Fenced action intents: `prepare_action_intent` and `transition_action_intent` take the epoch.
  PREPARED to EXECUTING, which authorizes a side-effecting handler to start, is refused for a
  stale owner. For a fenced running run every intent write without an epoch is refused, whatever
  it does: nothing outside the executor can start a handler or record SUCCEEDED, FAILED or
  CANCELLED, so no stale outcome becomes a receipt and an unknown outcome stays EXECUTING for
  recovery. After recovery blocks the run, an operator reconciles UNCERTAIN actions with the
  existing contract. The platform `ToolExecutor` uses `FencedActionIntents` whenever its
  `DurableActionScope` carries an epoch, and refuses a side-effecting tool whose store cannot fence.
- Scheduler. After any refusal the scheduler remembers it, writes nothing further, stops
  dispatch, cancels the other workers (including one that swallows the error), releases its
  resources and raises the original refusal. It never records a failure, a retry, a completion or
  a cancellation through a dead epoch.

## Guarantees and non-guarantees

1. Atomic protected writes (implemented, tested). A task, run or intent write using an expired or
   superseded lease is rejected when its transaction evaluates the lease row, including when no
   successor exists and when the run record still names the old owner.
2. Protected dispatch authorization (implemented, tested, narrow). The RUNNING write is fenced, so
   a controller that lost ownership before it cannot start newly authorized work, and nothing is
   dispatched after a refusal. A successful RUNNING write is not a lease: the lease can be lost
   before the worker body begins, and the worker is then invoked once, unable to commit anything.
   A database transaction cannot atomically guard an arbitrary worker invocation.
3. Side-effect admission (implemented, tested, narrow). The platform's fenced claim rejects a
   stale owner before a new side-effecting tool operation is authorized. A handler already
   admitted keeps running and its external effect is not undone. If it cannot record its outcome
   the intent stays EXECUTING for `RuntimeRecoveryManager` to mark uncertain; it is never turned
   into SUCCEEDED, FAILED or a safe cancellation by a controller that lost ownership. A restarted
   run blocks on the unresolved action. Worker code that does not use the platform tool executor
   is not covered and needs its own enforcement.
4. Replay identity (existing behavior). Deterministic task ids give one row per task. They do not
   prevent the worker body from running again; the scheduler is at-least-once.
5. External exactly-once effects: not guaranteed. A stable application idempotency key, durable
   receipts, external deduplication and reconciliation remain necessary; an equivalent call under
   a new call id and no key runs again.

Also not covered: durable approvals are not epoch-checked (an approval only permits one specific
intent and the claim is fenced); operator paths for runs that are no longer RUNNING remain
conditional on run status only; the single-host clock assumption above; a controller whose event loop
is blocked longer than the lease TTL simply fails closed on its next write.

## Operational restrictions

Supported: `SQLitePersistence` on one local file, `SQLiteLeaseStore(persistence)` over that same
object, and `FencedExecution(persistence=..., leases=...)` passed to `WorkflowController(
fenced=...)` together with the controller's `leases`. The controller refuses runs, action intents
or schedulers that are not that authority. `RedisLeaseStore` keeps working for existing,
non-fenced workflows and is not accepted here.

## Migration and compatibility

Additive: two tables (`fence_meta`, `fence_leases`) created if missing, and an optional
`fenced` flag on the run record that defaults to false, so existing databases and records load
unchanged and older code ignores them. Existing `save_task`, `save_runtime_run`,
`prepare_action_intent` and `transition_action_intent` behave as before when no epoch is given
and the run is not fenced. The `TaskStore` and `RuntimeRunStore` protocols are unchanged; fenced
support is an optional, explicitly declared capability (`fenced_execution_contract`).

## Integration requirements for pull request 14

Build the delegation integration on this foundation: require `fenced=` for delegation, compose
the scheduler over `FencedTaskStore`, drop the extra `renew` checks that only approximated this
guarantee, and replace the strict `xfail` reproduction with a passing regression in which the
lease is replaced after the last check and the stale controller neither persists nor executes
the delegated child.
