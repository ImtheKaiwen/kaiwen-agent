# Optional agent teams

Teams are an optional composition layer. They do not replace `AgentRuntime`, execute
models, schedule tasks, or grant authority.

`TeamDefinition` pins one lead and a finite set of member roles to exact registered
`AgentDefinition` versions. Each role declares a capability ceiling that must be a
subset of the registered agent permissions. `TeamDefinitionRegistry` is immutable at
an existing `id@version`.

`TeamRuntime.delegate()` chooses an explicitly requested role or deterministically
selects the least-privileged registered member that satisfies the request. It then
passes a normal `DelegationRequest` to `SubagentDelegator`. The existing central
`DelegationPolicy` remains responsible for delegate allowlists, depth, concurrency,
per-run limits, and capability intersection. `JobManager` and `TaskScheduler` remain
the only job and task execution authorities.

Context handoff accepts named references, not raw child histories. Reserved pinning
references bind each job to its team, member version, and role. Completed work is
returned as `TeamHandoff`, which includes only a bounded summary, artifact/evidence
references, blockers, and follow-up suggestions. Arbitrary worker result fields,
private scratchpads, and logs do not cross the child boundary.

Nested delegation is available only to members explicitly marked `may_delegate` and
must name the owned parent job. Resource conflicts continue to use Task Runtime
resource keys. Dynamic template instantiation and unrestricted agent synthesis are
not supported.

## Agents as workflow workers and team context

`AgentRuntimeWorker` runs one configured `AgentRuntime` as a workflow `Worker` through the
existing `TaskScheduler`; it does not add a runtime, scheduler or store. See ADR 0014.

- Identity and authority. The compiled task supplies identity that is re-checked against the
  durable run (`trusted_task_run`). Permissions, tool names, the role and the workspace
  authorization come from the host that built the worker, never from `task.input`, task
  capabilities or metadata. A task asking for capabilities the worker was not allowed is
  refused before any model call.
- Four scopes. Project state is the durable run, objective and evidence; team state is the
  run's `AgentTask` records; agent state is a private context; the Lead view is a bounded,
  read-only projection of team state, not a merged transcript (see "Lead coordination view").
- Private context. Each task gets a host-derived conversation id (`private_conversation_id`,
  from the run, role and logical task; `private_scope="role"` shares one thread per role), so
  a Coder's history is not a Tester's. A shared conversation id is never used.
- Handoffs. A `TaskContextPackage` (rendered through the existing `ContextBuilder`) carries
  bounded `TaskHandoff`s read from completed dependency tasks of the same run and owner.
  Summaries, objectives and descriptions are marked untrusted. A referenced artifact counts
  as verified only if it is readable in the run's owner scope, belongs to the run, and carries
  a trusted evidence origin; other references are dropped and counted.
- Results. The worker returns a bounded, handoff-shaped result with `verified: false`. Only
  the completion gate verifies anything; a model's claim of passing tests changes nothing.
- Writes stay serialized through resource keys; independent reads can overlap.

## Lead coordination view (read-only)

`LeadViewAssembler.assemble(run_id=, identity=)` returns a bounded `LeadView` of one durable run,
or `None` if that owner has no such run. It reads existing state only (ADR 0015, proposed):

- Host facts: run id and status, workspace binding id, task ids and logical ids, statuses,
  dependencies inside the listing, attempts, progress, status-derived blocker categories,
  pattern-checked error codes, the run's blocker category, and references the host verified
  with the same ownership and evidence-authentication rules as handoffs
  (`TaskContextAssembler.owned_references`).
- Untrusted text: task names and summaries (`untrusted_name`, `untrusted_summary`), with control
  characters removed and a length bound. They are rendered as data and grant nothing.
- Never exposed: exception text, task input or description, metadata, transcripts, private
  conversations, permissions or capabilities. The view reports no requirement verdict
  (`requirement_status` is always `not_evaluated`); authenticated evidence is not proof that a
  requirement passed, and the completion gate stays authoritative.
- Explicit absence: `unavailable_sources` lists stable `source:reason` codes for everything
  that was not configured or could not be read (`approvals:not_configured`,
  `approvals:incomplete`, `approvals:store_error`, `artifacts:not_configured`,
  `artifacts:store_error`, `evidence:not_configured`, `evidence:no_signer`,
  `evidence:store_error`). Pending approvals are `None` when unavailable, never an empty
  list. Per task, `artifact_check` and `evidence_check` are `checked` (an empty reference
  list is then a real result) or `unavailable` (empty says nothing). Claims that failed
  verification are counted in `unverified_reference_count`; claims that were never evaluated
  because verification was unavailable are counted in `unchecked_reference_count`. Artifacts
  without an evidence signer give `artifact_check=checked`, `evidence_check=unavailable`. A
  truncated listing and dropped records are reported (`tasks_truncated`,
  `unreadable_task_count`).
- Read-only and fail-closed: building the view writes nothing, runs no tool and takes no
  lease. The assembler requires a task store with the `TaskRunQuery` capability and refuses
  to fall back to the global `list_tasks`. `expected_workspace` refuses a run bound to another
  workspace. `LeadViewSource` feeds the view through the existing `ContextBuilder` budget.
- Bounded however the store behaves: at most `max_tasks` rows are read, each continuing page
  must make progress and advance its cursor, and a page that returns repeated or out-of-order
  tasks, more rows than requested or malformed fields is rejected (`task_listing_invalid`)
  instead of looped on.
- Sanitized failures: a failing run, task or approval store never escapes as a raw exception.
  `assemble` raises `LeadViewError` with a stable code (`run_store_unavailable`,
  `task_store_unavailable`, `task_listing_invalid`, `workspace_binding_mismatch`,
  `assembly_failed`) when ownership or completeness cannot be established, and optional
  sources become `unavailable_sources`. Logs carry only the code, the exception class name and
  the run id, never the exception message.
- Ownership of the listed tasks is verified membership in an existing run of the owner, not
  proof of who wrote them; the trusted-write boundary is `TaskStore.save_task` (ADR 0015).

## Delegation admission (workflow, preliminary)

`WorkflowDelegationAdmission.evaluate(run_id=, identity=, parent_task_id=, proposal=)` answers
"would this Lead proposal be admitted now" (ADR 0015, accepted). It validates and decides; it
does not act. It creates no task, writes no proposal, revises no plan, calls no scheduler or
`JobManager` and touches no approval. The only writes are what the central `PolicyEngine`
itself records for its own audit trail.

- Authority. A `WorkflowDelegationProposal` holds only plan-authored task specs; unknown fields
  (owner, depth, parent, permissions, authorization) are rejected. The owner and run come from
  the durable store for the host's identity, the parent task is a host record found in the run's
  attested listing (`parent_task_id` must come from trusted context, never from the proposal),
  and the parent's `delegation_depth` is read from it. The child's depth is the parent's plus
  one, computed by the host and reported as `child_depth`.
- Delegator authority. A parent task's recorded `capabilities` are what a plan asked for (the
  compiler checks them only against the host-wide set; an `AgentRuntimeWorker` checks them
  against its `allowed_capabilities` only when it executes the task), so they prove nothing about
  who may delegate. The host passes `delegators`, a mapping of `DelegationAuthority(worker_id,
  role, capabilities)` keyed by the parent task's worker id (compose it from the team members
  with `may_delegate`, or by hand). It has no default: with no entry for the parent's worker the
  answer is `delegator_not_authorized`, even if the central policy allows the target worker,
  because the delegate allowlist is not the caller's permission to delegate. The entry's
  `capabilities` are the delegation ceiling and are what the shared admission and the central
  policy see as the caller's capabilities (the role is shown to policies as `delegator_role`
  and must equal the registered worker's own role when it declares one, else
  `delegator_role_mismatch`). Capability labels and tool permissions are separate contracts and
  are never converted into each other. More than one worker can be authorized, which supports
  nested delegation within the existing depth limit; nothing is keyed to the name "lead". The
  recorded labels of a delegated parent (depth of at least one) additionally narrow its children.
- Checks, in order: owner, session and running run; the expected workspace binding;
  the proposal size; a bounded, validated scan of the run's tasks (a truncated or
  contract-violating listing fails closed); the parent and its state (`parent_statuses`, default
  running); the host authority and role of the parent's worker; worker, host-capability and
  resource-key allowlists of the `WorkflowCompiler`; the compiler's own structural validation as
  a pure dry run; then, per task, the shared `DelegationAdmission` (child capabilities within
  the delegator's ceiling, the central `agent.delegate` policy with the caller's active and
  per-run delegated-task counters, each task counting the ones before it).
- Outcomes. Only an explicit allow admits. Deny, a policy error, missing or malformed state and
  any storage failure are `denied` with a stable `code` and deterministic `reason` (no exception
  text, nothing logged but the code and exception class). Require-approval, review and modify
  are `approval_required`; they never admit and never rewrite the proposal. A later denial
  outranks an earlier approval requirement.
- Not a reservation, not final. Every decision has `preliminary` and `reserves_nothing` set to
  true. Concurrent evaluations can all be admitted, and a later evaluation reads fresh state.
  `WorkflowCompiler` validates structure, workers, host capabilities and resource keys but does
  not evaluate the policy or know live delegation quotas, so it is not a final authorization.
  The authoritative final admission, with fresh policy and limit checks where work becomes
  executable, belongs to the controller integration (PR-C1). Per-run counters count the
  caller's delegated tasks in this run; the job path counts across runs.

Not part of this layer yet: the cycle-boundary integration with an explicit revision reason
and the final authorization (PR-C1), model-facing proposal tools and durable idempotent
proposals (PR-C2), bounded cross-cycle handoff (PR-C3), reviewer approval and team acceptance
(M6.3.4), isolated parallel writers or worktree merging.
