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
