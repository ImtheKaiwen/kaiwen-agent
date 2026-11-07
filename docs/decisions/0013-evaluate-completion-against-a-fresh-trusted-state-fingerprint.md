# ADR 0013: Evaluate completion against a fresh, trusted state fingerprint

- Status: Accepted

## Context

`WorkflowController.execute` received one `protected_input_fingerprint` and used it for
every completion-gate evaluation. In a coding workflow the coder changes the project
after tests ran, so evidence recorded for state A could satisfy a gate that was meant to
describe state B, or, when the fingerprint was the objective text, nothing could ever
match source bytes. Command evidence also needs a trusted run id, but a workflow worker
only received an `AgentTask`, and no primitive built an owner-scoped tool context from the
durable run.

## Decision

- Add an optional `fingerprint_provider` to `WorkflowController`. Without it behavior is
  unchanged. With it, every gate evaluation uses a value the host's provider produces at that
  moment; the argument to `execute` becomes the stable objective/input identity and is not
  used for the gate. A provider failure or an empty value blocks the run.
- Choose a provider over a per-cycle binding or a new field on the plan. The state lives in
  the host's workspace, only the host can read it under its path policy, and a callable is
  the smallest extension that keeps non-coding workflows and fixed-input callers unchanged.
  Nothing a model supplies can reach it.
- Bracket a passing evaluation: ask the provider again and refuse completion if the value
  changed. This is detection, not atomicity.
- The coding implementation is `WorkspaceFingerprintProvider`, backed by
  `SnapshotBuilder.fingerprint()`, which uses the same walk and exclusions as an execution
  snapshot, so it equals the fingerprint of the bytes a sandbox run is attributed to.
- Add `trusted_task_context`, which derives an `AgentContext` from a compiled task after
  checking the owner and run against the durable run record, and
  `TrustedEvidencePipeline`, which wires one artifact manager and one signer into the
  recorder and the signed verifiers.

## Consequences

- Evidence for one source state never satisfies another; restarting a blocked run does not
  reuse earlier authority unless the same bytes are tested again.
- Workflows that keep a fixed fingerprint retain the earlier semantics, including that stale
  evidence can satisfy them. Coding hosts must configure the provider.
- A change made after the final provider read, or by a process that races the provider and the
  gate, is not detected. The provider reads the workspace at a point in time.
- Each gate evaluation hashes the workspace twice, bounded by the snapshot limits.
- Identity propagation stops at the context primitive: running a model agent as a worker is
  a later milestone.
