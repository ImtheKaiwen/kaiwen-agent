# ADR 0014: Hybrid team context and AgentRuntime workers

- Status: Accepted

## Context

M6.3.1 gave workers a trusted run identity and gave the completion gate a fresh state
fingerprint, but nothing ran a configured `AgentRuntime` as a workflow `Worker`, a run was
tied to an owner but not to a workspace, and there was no rule for what one agent may see
of another's work. A shared conversation id would put a Coder's working history in front of
the Tester and Reviewer, and a shared transcript would grow without bound and mix
model-authored claims with host facts.

The audit of the existing code found what can carry this without a new store:

- `TaskScheduler` already owns dependencies, resource keys, concurrency, retries, timeouts
  and cancellation, and persists each `AgentTask` (including its `output`) in the `TaskStore`.
- `WorkflowCompiler` is the only writer of `AgentTask` identity and `metadata`; plan-authored
  data lives in `task.input`.
- `DurableRunManager` persists the run record as JSON, with a tenant and user scoped read.
- `ArtifactManager` and the signed evidence pipeline already hold owner- and run-scoped
  artifacts; `ContextBuilder` already assembles bounded, prioritized context from sources.
- `AgentRuntime.run` merges `identity.metadata` with its own (the runtime's win), so reserved
  host fields must be set by the host and never come from model input.

## Decision

The team model is Private Agent Context + Shared Durable Project State + Bounded Task
Handoffs + Lead Coordination View. There are four conceptual scopes.

1. Project scope: the authoritative requirements, decisions, workspace revisions and
   evidence references. Today these are the durable run record (owner, workspace binding),
   the objective/standards, the signed evidence artifacts and the workspace fingerprint.
2. Team scope: shared task statuses, dependencies, handoffs, blockers and approved
   decisions. These are the `AgentTask` records of one run in the `TaskStore`; no second
   store is added.
3. Agent scope: the private conversational and working context of one role or task. Its
   conversation id is derived by the host from the run, role and logical task, so two agents
   never share a conversation by accident, and a handoff is the only way work crosses.
4. Lead scope: a bounded operational projection of team scope (statuses, handoffs,
   blockers), not a merged transcript. Its coordinator belongs to M6.3.3; M6.3.2 only
   provides the pieces it will read (`TaskHandoff`, the validated references).

Authoritative records are host-written: identity, run and workspace binding, task
status, dependency structure, scheduler outcomes, and signed evidence. Everything a model
or a plan wrote (objectives, task descriptions, acceptance text, summaries, claimed test
results, artifact references) is untrusted information. A `TaskContextPackage` keeps the
two groups separate, renders the untrusted group as data, and grants no permission.

Implementation boundaries:

- `AgentRuntimeWorker` adapts a configured `AgentRuntime` to the `Worker` protocol. It
  re-checks identity against the durable run (`trusted_task_context`), builds the runtime
  identity itself (permissions from the host, never the task), uses the existing runtime
  loop, and returns a bounded result with the keys `TeamRuntime.wait_handoff` already reads.
  A model reply is reported as `verified: false`; only the completion gate verifies.
- A run is bound to a workspace by `WorkspaceBinding` (workspace id, root, policy checksum)
  stored in the durable run record, set once while the run is created, immutable afterwards,
  and checked by the controller and by coding workers. An optional `identity_digest` binds the
  run to a host-assigned, opaque project identity (`AuthorizedCodingWorkspace(identity=...)`).
  Without it, two different directories with the same workspace id, root alias and policy
  produce the same binding; with it they do not, and relocating the same project keeps the
  binding because no path is stored. A worker that can produce evidence requires an identity. The field is optional, so existing
  records and non-coding workflows are unchanged. No parallel run database is created.
- A coding worker that can produce execution evidence must be given an
  `AuthorizedCodingWorkspace` whose fingerprint provider, command catalog, runner, recorder
  and signed verifiers all refer to that one workspace and to the controller that gates
  the run; any inconsistency fails at configuration. Such a worker is also refused without a
  controller, so the controller-specific checks cannot be skipped.
- A handoff artifact reference is kept only when it is readable in the run's owner scope and
  was created by the same run; an artifact of another run of the same user is dropped.
  Sharing across runs is not offered; it would need an explicit, authorized policy.
- A handoff evidence reference is kept as authenticated only when its HMAC verifies with the
  existing M6.2 `EvidenceSigner` (owner, run, content and labels), its origin label and
  payload name the trusted origin, and the payload names this run. A label alone proves
  nothing. Authentication is not requirement satisfaction: evidence from an older workspace
  state is authentic but does not satisfy the gate. The completion gate stays authoritative.
  Without a signer nothing authenticates.
- Writes to a shared workspace stay serialized through resource keys; independent read tasks
  may run concurrently. No worktree merging or isolated writers are added.

## Consequences

- No new database: the shared state is the existing run, task and artifact records.
- A handoff is only as good as the task outputs and artifacts the host stores. The summary
  is model-authored and bounded; consumers treat it as a hint.
- A worker context is derived per task, so an agent that needs its earlier reasoning in a
  later task must be configured with role-scoped private context; it never inherits another
  agent's.
- Workspace verification is still point-in-time and not atomic (ADR 0013). Provider-backed
  evidence proves the state that was hashed, not that no one wrote afterwards.
- The Lead coordination view, delegation tools, reviewer approval and team acceptance are
  not implemented here. (Clarification, M6.3.3-C3: earlier-cycle handoffs reuse the same handoff
  contract and reference checks, selected from the run's stored lineage; private context is
  still never shared.)
