# ADR 0017: Reviewer assessment and host-controlled team acceptance

- Status: Proposed

## Context

After M6.3.3 a Lead can delegate across cycles and later tasks receive bounded handoffs, but
nothing distinguishes five different facts: a worker reporting that it finished, a reviewer
recording an assessment, the completion gate passing on technical evidence, a human authorizing a
delivery, and the whole team's delivery satisfying its contract. The controller completes a run
when the gate passes, and a model's "approved" or "tests passed" must never be a substitute for any
of the others.

What already exists and what was checked:

- Reviewer findings can ride in a task's own `output`, which the scheduler persists with COMPLETED
  in one (fenced) write; C2 already uses this for proposals. Task outputs are model-authored text
  until the host binds them; they are not an authority source.
- `CompletionGate` decides PASS, BLOCKED, APPROVAL_REQUIRED or CONFIGURATION_ERROR over signed,
  owner- and run-scoped evidence and the protected fingerprint (ADR 0013). It keeps that role.
- `DurableApprovalGate` and `DurableApprovalRecord` are one-time decisions bound to a tool action
  intent: owner, run, tool name, call id, arguments checksum and intent id. They cannot be reused
  as a team or delegation approval without changing their meaning, so they are not.
- Run transitions and task writes are fenced (ADR 0016); the controller's `_RunWriter` is the only
  completion path.
- `TeamHandoff` is a bounded, model-authored result envelope of the TeamRuntime path.

## Decision

1. A reviewer is an ordinary `AgentRuntimeWorker` with one more registered tool, `review.submit`
   (`ReviewTools`). The tool stages one strict, bounded `ReviewAssessment` in an execution-local
   `ReviewSession`; a successful execution finalizes a canonical, digested record into
   `output["review_assessment"]`. No table, parallel database or approval ledger is added.
2. The record is bound by the host, never by the model: run, owner, session, workspace, the
   reviewer's own task id and worker id, the host fingerprint, the exact reviewed task ids with a
   digest of each stored output, and only the cited evidence references the host authenticated.
   An assessment of work the host did not show, a self-contradicting approval and a conflicting
   second assessment are refused. Reviewer text is untrusted data.
3. `TeamAcceptance` is an opt-in controller configuration evaluated after a passing completion
   gate. It re-reads authoritative storage and returns one of `accepted`, `changes_required`,
   `approval_required`, `blocked` or `unavailable`; only `accepted` allows the existing fenced
   completion write, and anything else BLOCKS the run with `team_<status>:<code>`. The decision is
   recomputed, not stored. The gate keeps sole authority over technical requirements; a review never
   overrides it and a passing gate never overrides a mandatory review or a pending human approval.
4. Reviewers are recognized only by a host mapping of worker ids to roles (checked against the
   registered worker), never by a role or label on a task or in a record. Required tasks, minimum
   reviewers, evidence and fingerprint requirements are host policy. The newest reviewer cycle counts.
5. A human approval is a host-implemented, read-only port (`HumanApprovalPort`) answering for an
   `ApprovalSubject` whose digest binds owner, run, workspace, fingerprint, reviewed outputs and
   review digests. The platform persists no such approval. Pending is not an allow, denied blocks,
   an approval for another digest does not match, and a failing source is `unavailable`.
6. Approval-required delegation is not resolved here. It would need a durable proposal approval
   ledger (or a cycle cursor to resume after a decision), which is a new authority design. It stays
   blocked with `delegation_approval_required`, and a reviewer recommendation never approves it.

7. Independent audit R1 corrections (same decision, no new authority). After acceptance returns
   `accepted` the controller re-reads the protected fingerprint immediately before the terminal
   transition (a point-in-time check, not atomic with the database write). A review records the
   compiler-proven cycle of each reviewed task, so a same-cycle dependency is valid and a conflicting
   label fails closed. The acceptance assembler must use the authoritative task store and, while
   evidence is required, the platform completion gate's artifact manager and signer by identity. An
   approval cannot leave a criterion `unknown`.
8. R2: because evidence can be deleted or expire during a human approval wait with the fingerprint
   unchanged, a configured approval is followed by a second authentication of the cited evidence and
   a second completion gate evaluation before the fingerprint re-check. No new store or engine; the
   checks stay point-in-time, not atomic with the database write.

## Consequences

- No schema change and no new authority store; older code ignores the extra output key.
- The authority of an acceptance is exactly as strong as the stored task outputs, the fenced
  storage and the host's approval source. The digest detects change, not a forger who may already
  write tasks (the trusted-write boundary of ADR 0015).
- Restart: the same stored rows give the same decision, or a different one if anything covered
  changed; replay is at least once. Exactly-once execution, a persisted decision and atomic
  workspace reads are not provided.
- `TeamRuntime`, `JobManager` and the Coding Team facade are unchanged; "accepted" is defined once,
  by `TeamAcceptanceDecision`.
- Open for a later milestone: a first-party approval store for team acceptance and approval-required
  delegation, each of which needs its own accepted design.
