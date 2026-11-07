# ADR 0011: Make cancellation, idempotency, and run ownership explicit contracts

- Status: Accepted

## Context

Reliability probes of the merged runtime showed that cancelling an `Agent` run left its
record `running`, cancelling `TaskScheduler.run()` orphaned worker tasks and resource
locks, `WorkflowController` left the durable run `running`, and `JobManager.wait()`
swallowed its caller's cancellation. Two concurrent identical side-effecting calls ran
the handler twice, and an idempotency key could be reused with different arguments.
A durable run that was `RUNNING` when its process died stayed `RUNNING` forever, and
jobs sharing a resource across schedulers failed instead of waiting.

## Decision

- Cancellation is distinct from failure. `Agent.run` records the `cancelled` run status
  and emits `run.cancelled` (a backward-compatible addition to the Python, JSON Schema,
  and TypeScript event contracts; older consumers ignore unknown event types).
  `TaskScheduler.run()` cancels and awaits its workers, releases resources, and ends
  unfinished tasks as `CANCELLED`, also on internal errors.
- An interrupted side effect is never reported as safely cancelled. A cancelled durable
  action becomes `UNCERTAIN` (`cancelled_in_flight`) and is never replayed.
  `WorkflowController` records `CANCELLED`, or `BLOCKED` with `uncertain_action:<id>`
  when it is given the action-intent store and an intent for the run is executing or
  uncertain. Without that store cancellation is recorded as `CANCELLED`.
- Idempotency reuses the action-intent system. Identical scoped operations are
  single-flighted inside a process, and cross-process exclusion stays the atomic claim
  of the durable store. The action-intent id no longer includes the arguments checksum,
  so reuse of a key with different canonical arguments is detected and rejected
  (`IdempotencyConflictError`) in flight, after completion, and on the durable path.
- `AgentRuntime` refuses autonomous side-effecting tools unless its `ToolExecutor` has a
  durable action-intent store. Local, supervised, and read-only use needs no durable
  infrastructure.
- Fail-fast tool errors remain the default. `tool_errors="return"` is opt-in and returns
  only sanitized, correctable errors (invalid arguments, unknown tools, failures of
  read-only tools), bounded by `max_tool_errors`. Permission, policy, approval,
  path-policy, conflict, and side-effect failures are never returned to the model.
- Run ownership is a lease. A controller configured with a `LeaseStore` heartbeats a
  per-run lease, records its key on the run, and stops without writing state if the
  lease is lost. Recovery blocks `RUNNING` runs whose recorded lease nobody holds
  (`owner_lost`); runs without a lease key are never guessed at, and nothing resumes
  automatically.
- Transient provider failures are classified by `is_transient_provider_error` inside
  `FailoverModelProvider`, so shipped adapters fail over without changing for direct
  users. Data-locality and provider-state affinity rules are unchanged.

## Consequences

- Action-intent ids written before this change used a different derivation, so a
  pre-upgrade in-flight intent will not be matched by a replay after the upgrade.
- Process-local single-flight does not coordinate separate processes without the
  durable store, and the in-memory lease store only protects a single process.
  Multi-node ownership needs a shared lease store such as the Redis adapter.
- Cancellation lookup of in-flight intents filters client-side over the first 1,000
  executing or uncertain intents.
- A scheduler waiting on a resource held by another scheduler polls every 20 ms and has
  no wait timeout; it is cancellable.
