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
idempotency key runs again. A restart re-authorizes from scratch (there is no durable cycle
cursor), and an identical proposal can be denied because the earlier delegated tasks count against
the per-run limit. Delegated tasks cannot depend on the parent or on earlier cycles; earlier-cycle
context reaches them only as informational handoffs (see the last section before Recovery scope).

## Lead tools and durable proposals (opt-in, M6.3.3-C2)

A Lead agent coordinates through two registered tools and gains no authority from them. The host
builds `LeadTools(config=LeadToolConfig(roles=..., resource_allowlist=..., max_tasks=...),
assembler=LeadViewAssembler(...))`, registers `tools.definitions()` in the Lead runtime, grants
the permissions `lead.view` and `lead.propose`, and builds the Lead as
`AgentRuntimeWorker(..., tool_names=[...], lead=tools)`. Lead tool names without `lead=` (or a
partial tool set with it) are refused at construction.

- `lead.view(offset, limit)`: one page (at most 50 tasks) of the existing `LeadView`. Run, owner,
  session and workspace come from the worker's per-execution `LeadToolSession`, which is built from
  the verified durable run; the arguments carry no such field and the tool refuses an execution
  whose identity differs from the session's. Names and summaries stay labelled `untrusted_*`,
  evidence is only authenticated, never judged, and sources that could not be read are reported
  as unavailable, not empty. It creates nothing and reserves nothing.
- `lead.propose_task(id, name, description, role, acceptance_criteria, dependencies,
  capabilities, resource_keys)`: strict schema, extra fields rejected, every field bounded. The
  host maps `role` to a worker id and a capability ceiling (`LeadRoleBinding`); a role, capability
  or resource outside that binding, an unknown or forward dependency, a count above `max_tasks` or
  an oversized task is returned to the model as `rejected` with a stable code and is not staged.
  Dependencies may only name tasks already proposed in the same set.

Proposal identity is `sha256(run, parent task, tenant, user, session, workspace, canonical task)`;
the tool-call id is not part of it, and the canonical form is order-insensitive for sets. An
identical proposal is idempotent (`duplicate: true`); the same logical id with other content is
`rejected` (`logical_id_conflict`) and the first stays.

Commit boundary. Staging writes to an execution-local sink only, and the tool reports
`committed: false`. After a successful run the worker seals the sink and returns the canonical set
as `output["delegation_proposals"]` (version, run, parent, owner, workspace, count, tasks, digest).
The scheduler then persists that output with the COMPLETED status in one task write, through the
fenced task store in fenced mode, so an expired or replaced owner cannot commit it. A failed,
cancelled or crashed Lead never finalizes a set. A fresh sink is built for every execution,
including retries and concurrent executions of one worker. If a tool result reported during this
execution names a staged proposal that is not in this execution's sink (a cached success from an
earlier attempt), the finalization fails with `staged_proposal_missing` instead of committing a set
without it.

`DurableLeadProposalSource(tasks, runs, config, lead_workers, expected_workspace)` is the
`WorkflowDelegationSource`. For each eligible parent that is a Lead worker it re-reads the task
from storage and requires: completed in the current cycle, the same run, owner and session as the
verified durable run, the expected workspace, a present record (a Lead without one is
`record_missing`, never read as "no proposal"), a matching digest, a count equal to the listed
tasks, unique logical ids, canonical content, and the host's current role, capability and
resource bindings. Any failure raises `LeadProposalError` with a stable code, which the controller
reports as `delegation_source_failed`. An empty record is a real "no proposal". Two Lead parents
with proposals in one boundary are refused (`multiple_proposal_sets`) rather than one being
dropped. The source writes nothing; the controller still runs the fresh final admission (host
authority, ceilings, central policy with live counters, compiler) and a stored record is never an
authorization.

Composition (checked when the controller is built). A `DurableLeadProposalSource` must read tasks
from the controller's own fenced database (`fenced.persistence` or `fenced.task_store`, by object
identity), use the controller's own `DurableRunManager`, and expect the controller's workspace
binding. Every Lead worker (one built with `lead=`) must be listed in `lead_workers` and use the
same `LeadToolConfig` as the source, so no Lead commits proposals that nobody reads or that the
reader would reject. A worker built with `lead=` must have registered the `LeadTools` objects
themselves and hold the `lead.view` / `lead.propose` permissions. Incompatible configurations are
refused before any work runs. A custom `WorkflowDelegationSource` keeps the host-controlled
contract and is not inspected.

Element bounds. Besides the counts (10 criteria, 10 dependencies, 10 capabilities, 5 resource
keys), each member is bounded: criteria 1 to 500 characters, dependencies a task id of at most
200 characters, capabilities a label of at most 100 characters, resource keys at most 200
characters. The same model validates a committed record when it is read, which also checks the
canonical form, 8,000 bytes per task (canonical JSON escapes non-ASCII text) and the record size.

Limits. The digest detects truncation and accidental corruption; it is not a signature, and a
writer already trusted to write tasks can forge a consistent record (the existing trusted-write
boundary, ADR 0015). Idempotent proposals are not exactly-once execution: a restart re-runs the
Lead (at least once, under the same deterministic task ids) and re-authorizes. No quota is
reserved, no workflow cursor resumes a run, and only a Lead completed in the cycle that just
ended can be a parent. Records are limited to 50 tasks, 8,000 bytes per task and 128,000 bytes in
all.

## Earlier-cycle handoffs (opt-in, M6.3.3-C3)

A task compiled in cycle N has no graph edge to an earlier cycle and gets none: the compiler still
refuses foreign-cycle dependencies. Instead the host can hand it bounded, informational context from
earlier cycles of the same durable run:
`TaskContextAssembler(..., cross_cycle=CrossCycleHandoffs(tasks=<TaskRunQuery store>, roles=...,
expected_workspace=..., max_sources=4, max_scan_tasks=200))`, or `AgentRuntimeWorker(...,
cross_cycle=...)` (with `evidence_signer=` when there is no authorized workspace to supply it). The
store must have the optional `TaskRunQuery` capability (the fenced SQLite store does); an in-memory
or custom store without it is refused at construction, and `max_sources` (1 to 10) and
`max_scan_tasks` (1 to 500) are validated. The source must also be the assembler's own
authoritative task store: the same object, or the platform's own `FencedTaskStore` (recognized by
its exact type; a subclass is refused because it may override reads) and the one persistence it
wraps. Matching ids, protocol conformance, a path or a capability flag never
count, and another handle on the same file is another object. Both `TaskContextAssembler` and
`AgentRuntimeWorker` (as `WorkerConfigurationError`) refuse anything else at construction, before
any earlier-cycle content can be read.

Eligible sources. Only a task with delegation lineage (a compiler-written `parent_id` and a depth of
at least one) receives anything; other tasks, and workflows without the option, are unchanged. The
sources, in this order, are: the delegation parent; the other completed tasks of the parent's
delegation round (same parent, same depth, same cycle) in task-id order; the parent's own parent.
The list is cut at `max_sources`. Ordering is by task id, never by a timestamp, and the model
supplies no source id. Every source must belong to the run's tenant, user, session and durable run,
carry an id the compiler derives from its stored run, cycle and logical id (so forged cycle or
logical-id metadata is rejected), be in a strictly earlier cycle than the receiver, and be COMPLETED
with an output record. Lineage must be consistent (depth steps of one, cycles strictly decreasing).
Failed or unfinished siblings are not sources; a sibling whose cycle claim its id does not prove is
rejected and counted.

Provenance. Each handoff is a `TaskHandoff` marked `origin="prior_cycle"` with the source task id,
logical id, cycle, worker id (from the stored task), `relation` (`parent`, `cohort`,
`grandparent`) and the role the host maps to that worker (never the role the task claims). The
summary is clipped, model-authored and rendered under an "untrusted data" header; blockers are
carried; follow-up suggestions of an earlier cycle are not. The package's `cross_cycle` summary says:
`complete` (every completed source the rule names is included and none is left out);
`truncated` (completed sources were left out only by `max_sources`, with the number left out,
and every included source is a valid handoff); `unavailable` (the host cannot trust the
selection, so no earlier-cycle handoff is shown at all; stable reasons: `inventory_incomplete`,
`inventory_unavailable`, `inventory_invalid`, `parent_not_found`, `parent_not_earlier`,
`parent_not_completed`, `lineage_broken`, `lineage_incomplete`, `task_identity_invalid`,
`workspace_mismatch`, `source_identity_invalid` for a row in the lineage position whose cycle
its id does not prove, and `source_output_missing` for a selected completed source without an
output record); or `not_applicable`. A selected source with no output record never leaves a
partial set marked complete. A valid empty record is a valid handoff, a valid task of another
delegation round is simply not a source, and a dropped artifact or evidence reference is counted
as unverified without making the source missing. An unavailable result also adds a blocker line, so a missing or partial history cannot be mistaken for a complete one. The
host never silently picks a partial set: an inventory that is truncated by the scan bound, has
unreadable or foreign rows or violates the listing contract yields `unavailable`.

Verification and budget. Artifact references are re-read through the existing owner- and
run-scoped `ArtifactManager` rules; evidence references additionally need the existing
`EvidenceSigner` signature, trusted origin label and a payload naming this run. Dropped claims are
counted. Authentication is not acceptance: the package says so, shows the host's own current
fingerprint (never one a task claimed), and only the completion gate decides. Earlier-cycle items
rank below the required task items and below same-graph dependency handoffs in the existing
`ContextBuilder` budget. Work is one bounded scan plus the reference checks; there are no
per-source task lookups.

Guarantees and limits. Read-only: nothing is written, no ownership is taken and a stale owner is
still stopped by the fenced write that follows, as before. A failing inventory does not fail the
task: the context is informational and reports itself unavailable, while cancellation is never
swallowed. The read is point in time, not a snapshot: rows are listed once and artifacts and the
fingerprint are read afterwards, so a source row changed in between is seen on the next assembly.
A replay or retry rebuilds the same handoffs from storage (deterministic ids); an artifact store
that does not survive a restart makes its references drop and be counted. Not provided: cross-run
sharing, transcripts, a cycle cursor, resume, permissions or capabilities from history, or any
statement that earlier evidence satisfies a requirement for the current workspace state.

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
