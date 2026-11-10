# Autonomous workflows

`kaiwen-agent` exposes a bounded, single-agent workflow control plane. It composes
existing runtime services rather than creating another model loop or executor.

## Execution boundary

`WorkflowPlan` is untrusted input. `WorkflowCompiler` validates owner identity,
allowlisted workers, effective capabilities, resource keys, graph structure, limits,
and strict JSON task input before producing an existing `TaskGraph`. Only
`TaskScheduler` executes the resulting tasks and persists task state.

A `WorkflowRevisionPlanner` may propose a replacement plan after task or quality
failure. Every proposal passes through the same compiler. The planner cannot execute
tasks, grant permissions, alter the resolved standards, or declare completion.

## Completion and repair

`WorkflowController` evaluates the fixed `StandardResolution` through the existing
`CompletionGate`. Mandatory requirements need owner-scoped, current-input evidence;
an agent statement is not evidence. Repair and replan attempts are finite and stop on
repeated failure fingerprints, exhausted limits, approval requirements, invalid
configuration, or the workflow cycle limit.

Side-effecting workers must separately compose the durable U5 action-intent,
approval, permission, and idempotency pipeline. Workflow compilation does not grant
tool authority.

## Fresh state fingerprints and run identity

A workflow has two different identities that must not be confused. The objective (and its
protected input) and the durable run id stay fixed for the whole run. The state of the
project changes whenever a worker edits it.

`WorkflowController.execute(..., protected_input_fingerprint=...)` keeps its meaning: with
no provider configured, that single value is passed to every `CompletionGate` evaluation,
exactly as before. A fixed value taken before the coder edits therefore lets evidence for
the earlier state satisfy the gate for the later one.

Hosts running coding workflows configure `WorkflowController(fingerprint_provider=...)`
(`ProtectedInputFingerprintProvider`; `WorkspaceFingerprintProvider` in the Coding Kit). Each
evaluation then uses a freshly obtained, content-aware fingerprint of the workspace, and
the passed value only names the input. The controller also asks the provider again after a
passing evaluation and refuses to complete when the value changed
(`workspace_changed_during_verification`); a provider that fails or returns nothing blocks
the run (`protected_input_unavailable`). This detects a concurrent writer; it does not make
the workspace atomic, and the workspace can still change after the final check. The value
that was verified is reported as `WorkflowOutcome.verified_input_fingerprint`.

`trusted_task_context(task, runs=..., permissions=...)` builds the `AgentContext` a worker
passes to tools. The tenant, user, session and run id are re-checked against the durable
run record (it must exist for that owner and be running), the run id is never read from
`task.input` or tool arguments, the trace id stays distinct from the run id, and
permissions come from the calling host. It is only a context primitive; it does not run
agents or tools.

## Run-to-workspace binding and coding workers

A durable run can be bound to one workspace with `WorkspaceBinding` (workspace id, root, policy
checksum), passed to `DurableRunManager.create(workspace_binding=...)` or set once before
start with `bind_workspace`. It is stored in the durable run record, so it survives restarts,
the store refuses to change it, and old records without one load unchanged.
`WorkflowController(workspace_binding=...)` blocks a run whose durable binding differs
(`workspace_binding_mismatch`) before any task runs; without it nothing changes for other
workflows. A coding worker built with `AgentRuntimeWorker(workspace=AuthorizedCodingWorkspace(...))`
re-checks the binding of the run it executes for.

`AuthorizedCodingWorkspace` lists every inconsistency between the workspace policy, fingerprint
provider, command catalog, sandbox runner, evidence pipeline and controller. A worker that can
produce execution evidence (`evidence_tool_names`) is refused at construction unless the
controller uses that workspace's fingerprint provider and binding and its gate uses the
pipeline's signed verifiers. It is also refused without a controller or without a workspace
identity. Fixed-fingerprint workflows and workers that produce no evidence are not affected.

Workspace identity: the binding holds the workspace id, root alias and policy checksum, none
of which names a physical location. `AuthorizedCodingWorkspace(identity=...)` adds a
host-assigned opaque identifier (for example the project record id), stored only as a scoped
digest. Two projects with the same id, alias and policy but different identities do not match,
and the same project moved to another directory or machine keeps its binding. The host owns
the mapping from identity to directory; the platform does not verify that the directory is the
project, so give different projects different identities.

Handoffs: a dependency's artifact references are kept only when they belong to the same run
and owner scope. Evidence references are kept as authenticated only when the existing
`EvidenceSigner` verifies them for this owner and run. Authenticated evidence may be stale;
only the completion gate decides whether a requirement is satisfied.

## Delegation at the cycle boundary (opt-in)

`WorkflowController(delegation=WorkflowDelegation(source=..., admission=..., max_rounds=...))`
lets a host-controlled source propose more work after a cycle (ADR 0015). It is off by default
and changes nothing when absent. It needs fenced execution (`WorkflowController(fenced=FencedExecution(...))`, ADR 0016: one
SQLite database for leases, runs, tasks and action intents); configuration fails with a
`FencedConfigurationError` without it, with Redis, in-memory or custom lease and task stores, with
an admission over other runs or another task database, with an unfenced scheduler, and with
an admission built from a different compiler, with parent statuses other than completed, or with
a workspace binding other than the controller's.

After a cycle whose tasks all succeeded, and before the completion gate, the controller calls the
source with a `WorkflowRevisionRequest` whose reason is `DELEGATION_REQUESTED`. The source may
return a `DelegationCandidate` (a parent task id and an untrusted `WorkflowDelegationProposal`) or
nothing, in which case the gate judges completion exactly as before. A candidate must name a task
that completed in that cycle, and it is then freshly authorized: run and workspace, the parent's
worker in the host-owned `DelegationAuthority` mapping, the capability ceiling, the child worker,
the central policy with live depth and quota counters, and the compiler. The controller re-verifies
its lease and compiles the admitted tasks, and only those, as the next cycle, with the
parent reference and depth written by the compiler. A revision planner is never consulted.

Anything but an explicit allow blocks the run with `delegation_denied:<code>` or
`delegation_approval_required:<code>`; so do a failing source (`delegation_source_failed`), an
invalid candidate, an ineligible parent and an exhausted round or cycle limit. A pending or
rejected delegation is never a completion, a task failure or a gate failure, and the completion
gate alone still decides whether the run is complete, after the delegation rounds.

An incomplete task listing (truncated, unreadable rows, or rows not belonging to the run) denies
with `state_incomplete`. Before the delegated tasks are compiled and again before they are
scheduled, the controller asks the lease store whether it still holds the lease; the run record's
token alone is not proof. That is an early refusal. The authority is the owner epoch checked inside
every fenced task write, so a controller that lost the lease after its last check is refused at the
first delegated task write and persists and schedules nothing.

Limits. A fresh evaluation is not a reservation: the guarantee comes from the run lease and the
epoch enforced in the write transaction, not from the policy check, and external side effects are
not fenced by it. A task or tool call already authorized before the lease was lost may still have
effects afterwards. The guarantee is one local SQLite database, not multiple hosts, and not a
guard on arbitrary worker code. Replay is at-least-once: deterministic ids make one task row, not
exactly-once execution, and a tool call re-issued under a new call id without an application
idempotency key runs again. There
are no durable proposals yet, so a restart re-authorizes from scratch and an identical proposal
can be denied because the earlier delegated tasks count against the per-run limit (PR-C2).
Delegated tasks cannot depend on the parent or on earlier cycles (PR-C3).

## Recovery scope

The runtime run and scheduled task records are durable when the host supplies durable
stores. Recovery occurs at explicit safe boundaries with at-least-once semantics;
the platform does not claim token-level model-stream resumption. Interrupted external
effects remain subject to U5 reconciliation and are never retried merely because a
workflow is resumed.

`AgentRuntime.run()` intentionally continues to reject `RunMode.WORKFLOW`. Hosts use
`WorkflowController` with a structured, validated plan until a stable planning input
contract can be added without changing the chat fast path or duplicating the model
loop.
