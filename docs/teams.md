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

Not part of this layer yet: delegation tools and admission (later M6.3.3 work, pending ADR
0015), reviewer approval and team acceptance (M6.3.4), isolated parallel writers or worktree
merging.
