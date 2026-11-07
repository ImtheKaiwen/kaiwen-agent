# Background jobs

`kaiwen_agent.jobs` lets a host submit work without blocking the active text or
realtime conversation. It is a coordination layer over the existing Task Runtime,
not a second worker system.

## Composition

1. Register product-owned workers in `WorkerRegistry`.
2. Create `SchedulerTaskRuntimeAdapter` with the shared task store, resource manager,
   and run control used by the host.
3. Create `JobManager` with an explicit worker allowlist and a durable `JobStore`.
4. Build `JobContext` from authenticated server state, then submit a
   `JobSubmitRequest`.

`JobSubmitRequest` intentionally has no tenant, user, session, or requesting-agent
fields. Those authority-bearing values come only from trusted `JobContext`.

```python
reference = await jobs.submit(
    trusted_context,
    JobSubmitRequest(
        worker_agent_id="infrastructure-agent",
        objective="Check production health",
        input={"project": "campusmeal"},
    ),
)
```

Submission persists `queued` state, emits `job.created` and `job.queued`, schedules
the Task Runtime work, and returns immediately. Progress and terminal results are
projected back into the job record and emitted using canonical job events.

## Cancellation and recovery

Cancellation is cooperative. Running workers must call `checkpoint()` at safe
boundaries. Parent cancellation cascades to active children by default.
`JobManager.wait()` returns when the job finishes or is cancelled; cancelling the
coroutine that waits propagates normally and never cancels the job.

Call `JobManager.recover()` once while an application instance has exclusive startup
ownership. It atomically requeues non-terminal jobs and resolves persisted
`cancel_requested` records. Recovered work has at-least-once semantics; side-effecting
workers must continue to use approval and idempotency controls.

`InMemoryJobStore` is for tests. `SQLitePersistence` implements the reference durable
job port for local/small deployments. Distributed claiming, leases, heartbeats, and
stuck-job detection remain production-hardening work.

## Parent and child jobs

Pass an owned, non-terminal `parent_job_id` to create a child. Child progress is
rolled up to the parent.

## Subagent delegation

`SubagentDelegator` is the model-facing delegation boundary. It rejects capability
escalation before policy evaluation, then applies the central `DelegationPolicy`
allowlist plus depth, parallel-job, and per-run limits. Only an `allow` decision
submits work; modify, review, and approval decisions fail closed until a host composes
an explicit approval workflow.

Child capabilities must be a subset of both the trusted caller and parent job. Only
the worker assigned to an active parent may delegate its child. The selected
capabilities and delegation depth are carried into the typed `AgentTask` received by
the worker.

`TextJobBridge` and `RealtimeJobBridge` map trusted channel identity onto the same
`JobManager` and `SubagentDelegator`. The older `RealtimeTaskBridge` remains available
for compatibility, but new delegated background work should use the shared job
runtime rather than create a second task system.
