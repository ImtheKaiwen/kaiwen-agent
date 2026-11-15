# Coding team beta integration (M6.4-A)

Status: a bounded integration baseline, not a production release. It shows that the existing
platform composes into a coding team (Lead, Coder, Tester, Reviewer) whose delivery is completed
only by a real completion gate and a real team acceptance, without a second engine, scheduler,
database, authority system or approval ledger. Nothing here changes an accepted ADR or trust
boundary (ADR 0012 to 0017).

## Integration assessment

| Capability | State before this milestone | Now |
| --- | --- | --- |
| Fenced run ownership, SQLite leases, fenced task store, durable runs | implemented and tested | unchanged, composed by the example |
| Lead delegation (C1 to C3), compiler-proven ids, cross-cycle handoffs | implemented and tested | unchanged, composed by the example |
| Reviewer assessment and team acceptance (ADR 0017) | implemented and tested, but only through test harnesses | composed from public interfaces by the example |
| Trusted evidence pipeline, completion gate, protected-workspace fingerprint | implemented and tested (Docker examples, tests) | composed with a local runner by the example |
| Public host-composition interfaces | present: `WorkflowController`, `AgentRuntimeWorker`, `TeamAcceptance`, `AuthorizedCodingWorkspace`, `TrustedEvidencePipeline`, `CommandCatalog` | no new public API was needed |
| Missing consumer code | a maintained, runnable composition of all of them | `examples/coding-team-acceptance/main.py` |
| Needs a new decision | model-driven unattended test runs without Docker; model-driven file edits with durable approvals in a team; multi-process execution | not attempted (see Findings) |

## Run the example

Requirements: Python 3.11 or newer and the package itself (`pip install -e .`); no extra packages,
no API key, no network and no Docker engine. The default execution is deterministic.

```bash
python examples/coding-team-acceptance/main.py            # every documented scenario
python examples/coding-team-acceptance/main.py accepted    # one scenario
```

Expected output (the run, acceptance and reason columns come from the platform, not from the
script):

```text
[ok ] accepted                             -> completed run=completed reason=None acceptance=accepted:accepted
[ok ] same_cycle                           -> completed run=completed reason=None acceptance=accepted:accepted
[ok ] human_approved                       -> completed run=completed reason=None acceptance=accepted:accepted
[ok ] changes_requested                    -> blocked   run=blocked   reason=team_changes_required:reviewer_requested_changes acceptance=changes_required:reviewer_requested_changes
[ok ] failing_tests                        -> blocked   run=blocked   reason=quality_blocked acceptance=not_evaluated
[ok ] unrelated_evidence                   -> blocked   run=blocked   reason=team_changes_required:evidence_not_gate_verified acceptance=changes_required:evidence_not_gate_verified
[ok ] no_reviewer                          -> blocked   run=blocked   reason=team_blocked:review_missing acceptance=blocked:review_missing
[ok ] approval_denied                      -> blocked   run=blocked   reason=team_blocked:human_denied acceptance=blocked:human_denied
[ok ] approval_for_other_delivery          -> blocked   run=blocked   reason=team_blocked:approval_mismatch acceptance=blocked:approval_mismatch
[ok ] workspace_changed_during_approval    -> blocked   run=blocked   reason=workspace_changed_after_acceptance acceptance=accepted:accepted
Every scenario ended as documented; only the accepted deliveries completed.
```

A blocked row is the correct result for that scenario: the run is `blocked` and no delivery is
reported. `[BAD]` means a scenario did not end as documented and the command exits non-zero; an
unknown scenario name exits with 2. The last row shows `acceptance=accepted:accepted` together with a
blocked run: acceptance had decided before the workspace changed, and the controller refused to
complete against the new state, so read the `reason`, not the acceptance column.

## What is real and what is scripted

Real: SQLite persistence and leases (one database, fenced owner epochs), the fenced task store, the
durable run manager, the workflow compiler, controller and scheduler, `AgentRuntime` and
`AgentRuntimeWorker` (Lead, second Lead and Reviewer), Lead delegation with policy admission, the
cross-cycle handoffs, the command catalog, the evidence signer, artifact manager and verifiers
(`coding.tests` requirement), the protected workspace fingerprint, `CompletionGate`, `ReviewTools` and
`TeamAcceptance`.

Scripted or local: the model responses (`ScriptedModel`, the only stand-in for an external,
nondeterministic dependency); the human approval answers (`ExampleApprover`, a read-only
`HumanApprovalPort`); the Coder, whose edit is a host function writing through the real
`WorkspaceFilesystem`; and the Tester, a host-run step that executes two fixed command presets through
the real catalog and evidence pipeline. The test runner is `LocalSnapshotRunner`: it runs the tests
in a temporary copy of the workspace with the current Python and is **not a sandbox**.

## Configuration

* Workspace identity: `WorkspaceFilesystem(workspace_id, roots)` plus `AuthorizedCodingWorkspace`; the
  run is created with `workspace_binding=workspace.binding`, the controller, the delegation source and
  admission, and every worker that records identity expect that same binding.
* Permissions: each runtime gets only its own tool permissions (`lead.view`, `lead.propose`,
  `review.submit`); tools are deny-by-default.
* Role mapping: the Lead may only propose the roles bound in `LeadToolConfig`; the reviewer is
  recognized only by `TeamAcceptancePolicy(reviewer_workers={"reviewer": "reviewer"})`, never by a
  label in a plan or a record.
* Standards: `coding.tests` resolved from `coding_standards_packs()`; its requirement is verified
  against signed evidence for the current fingerprint.
* Evidence: `TrustedEvidencePipeline` with the bindings for `unit.tests` (the gate-verified
  requirement `coding.tests.run`) and `compile.check` (authentic, run-scoped evidence the gate does not
  rely on, used to show that a review citing it is refused).
* Coverage: `TeamAcceptancePolicy(required_tasks={"code", "test"})` (an empty set is refused).

Errors are reported as stable codes (`team_<status>:<code>`, `quality_blocked`, ...); no exception text,
key or model output is printed.

## Required scenarios and where each is pinned

| Scenario | Evidence |
| --- | --- |
| Successful execution (Lead, Coder, Tester, Reviewer, gate, acceptance, fenced completion) | `accepted`, `human_approved`; `tests/test_coding_team_example.py` |
| Same-cycle Coder, Tester, Reviewer; cross-cycle delegation and handoffs; proven cycles | `same_cycle`, `accepted`; the example tests read the stored cycles; `tests/test_team_acceptance_r1.py`, `tests/test_cross_cycle_e2e.py` |
| Superseded work; duplicate logical ids | `tests/test_team_acceptance.py`, `tests/test_cross_cycle_handoffs.py` |
| Reviewer missing; reviewer failed, silent or malformed | `no_reviewer`; `tests/test_team_acceptance_e2e.py` |
| Reviewer requests changes; incomplete coverage | `changes_requested`; `tests/test_team_acceptance.py`, `tests/test_team_acceptance_final.py` |
| Completion gate fails | `failing_tests` |
| Cited evidence unrelated to gate-verified evidence; another run; stale; unauthenticated | `unrelated_evidence`; `tests/test_team_acceptance_final.py` and `tests/test_team_acceptance.py` |
| Workspace changes during an asynchronous approval | `workspace_changed_during_approval`; `tests/test_team_acceptance_r1.py` |
| Approval missing, denied, for another digest; valid approval, unchanged workspace | `approval_denied`, `approval_for_other_delivery`, `human_approved` |
| Ownership lost during acceptance | `test_losing_ownership_during_acceptance_never_completes` (injected lease clock) |
| Cancellation | `test_cancellation_propagates_out_of_the_approval_wait` (barrier) |
| Safe replay after a blocked run | `test_a_blocked_run_can_be_replayed_and_is_decided_again_from_storage` |
| Legacy workflow without team acceptance | `test_the_acceptance_is_opt_in_and_a_legacy_flow_keeps_its_behavior` |

## Findings (confirmed integration gaps)

1. A model-driven `testing.run` may only run unattended on a runner that declares real isolation
   (`provides_isolation`, `DockerSandboxRunner`). A credential-free example without a Docker engine
   therefore cannot give the Tester that tool without lying about isolation, and it does not. The Tester
   is a host-run step that uses the same catalog and evidence pipeline. The model-driven Tester over
   Docker is demonstrated by `examples/coding-agent-worker` (CI, Linux); the combination with this
   multi-role team is not yet run anywhere.
2. `workspace.apply_patch` requires approval, and a run that waits for a durable approval has no
   resume path (general resume is not implemented). The Coder's edit is therefore a host function.
3. The team runs in one process under one owner lease; distributed execution is out of scope.
4. No new public API was needed; the example's only injection points (`approver`, `clock`) are
   parameters of the example function, and the lease clock is the existing `lease_clock` attribute.

## Real-provider readiness and manual validation specification

Already documented (`docs/live-validation.md`, manual, not CI-attested): one single-worker Tester
smoke on `gpt-5.4-mini` in a Docker sandbox, a passing and a deliberately broken project, with
signed evidence authenticated and the gate completing or blocking as expected, at about $0.002 per
scenario. These calls are not repeated here.

Unverified for a real multi-role workflow: a real model driving the Lead proposal tools, the review
tool (`review.submit` strict schema), and the model citing real evidence references; a real model
inside the full controller with delegation and acceptance; provider error behavior across several
roles; token and cost totals for a multi-task run.

A future manual validation (a separate run that requires explicit operator authorization and a stated
maximum cost) should specify:

1. Preflight (no spend): capability and configuration checks of the provider and model for tool
   calling and strict schemas; refuse to start if the Docker engine, the digest-pinned image or the
   key is missing; print only redacted configuration.
2. Limits: maximum requests across all roles, steps per task, tool calls per task, output tokens, input
   tokens, per-request and whole-run timeouts, and a hard budget; abort before executing tools when a
   limit is reached; never retry automatically.
3. Provider errors: classify as in `docs/live-validation.md` (rejected before transport, transport
   failure, HTTP error, adapter failure after response, valid response without the expected tool call);
   a classification of `undetermined` is reported, never guessed.
4. Positive: Lead proposes the Coder, Tester and Reviewer through `lead.propose_task`; the Tester runs
   `unit.tests` in Docker and signed evidence authenticates; the Reviewer submits one assessment citing
   an evidence reference; the gate passes; acceptance returns `accepted`; the run completes.
5. Negative: tests fail (gate blocks); the Reviewer is absent or requests changes (acceptance blocks); a
   model claim of success without evidence changes nothing.
6. Reporting: stable codes, token and request counts, latency and estimated cost only; no prompts,
   responses, headers, tool arguments or exception messages; `not_run` is never a pass.
7. Never fabricate a live result from the deterministic tests.

## Beta readiness

| Area | Status | Evidence and gap |
| --- | --- | --- |
| Installation and configuration | PARTIAL | `pip install -e .` runs the example; no setup guide for a real consumer beyond the examples |
| Coding workflow composition | READY (deterministic) | the example and its tests run the full team through the real stack |
| Provider compatibility | PARTIAL | one model, single-worker, manual smoke; multi-role not validated live |
| Tool permissions | READY | deny-by-default, per-role permissions, covered by tests |
| Sandbox execution | PARTIAL | Docker runner is tested in Linux CI; the multi-role team does not use it yet |
| Evidence verification | READY | signed, run- and fingerprint-scoped; gate-verified citations enforced |
| Team review and acceptance | READY (author-side) | ADR 0017 accepted, fresh-context review only, no author-independent audit |
| Durable state and recovery | PARTIAL | at-least-once replay and recovery reconcile exist; no general resume or cycle cursor |
| Cancellation and ownership | READY | fenced writes, injected-clock ownership loss, cancellation tests |
| Diagnostics and error reporting | PARTIAL | stable codes, no secrets; no unified operator view across roles |
| Documentation | PARTIAL | this guide and the workflow docs; no consumer quick start for a team |
| Packaging | PARTIAL | wheel and sdist build and check in CI; the example is not part of the wheel |
| Out of scope | not started | distributed execution, gateway, speech, UI, production deployment, release |
| Not implemented | missing | approval-required delegation, a first-party approval store, general resume, model-driven edits with approvals inside a team |

No claim of production readiness is made.

## Recommended next milestone

M6.4-B: run the same team with a model-driven Tester (`testing.run` over `DockerSandboxRunner`) and,
separately authorized, one bounded live-provider validation following the specification above. It is
the smallest step that closes the two PARTIAL items with real evidence (sandbox in the team,
multi-role provider behavior) without adding architecture.
