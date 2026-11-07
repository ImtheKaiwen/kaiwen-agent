# ADR 0012: Run untrusted project code in a Docker sandbox over verified snapshots

- Status: Accepted

## Context

Command presets existed, but no first-party runner shipped, so hosts either had no test
execution or wrote their own. The completion gate read evidence artifacts, but nothing
produced them from real executions, and `ProjectDiscovery.fingerprint` covers paths and
manifest contents only, so a source change did not invalidate evidence. A JSON artifact
carrying `status: pass` satisfied a verifier regardless of who wrote it.

## Decision

- Add `DockerSandboxRunner` behind the existing `SandboxRunner` protocol. It builds the
  `docker run` argument vector from constants and trusted host configuration, requires
  a digest-pinned image by default, never pulls, runs as a non-root user with no
  network, dropped capabilities, no privilege escalation, a read-only root, and CPU,
  memory, process, time, and output limits. It fails closed with
  `SandboxUnavailableError` and has no host-execution fallback.
- Commands run only against an `ExecutionSnapshot`: a validated private copy made under
  the existing workspace read policy, hashed from the bytes copied, copied again per run
  and re-verified. The live workspace is never mounted and cannot be modified by tests.
  The snapshot fingerprint (paths, sizes, content digests, modes) is the project
  fingerprint for evidence; discovery's fingerprint is unchanged and not used for it.
- `CommandCatalog` gains optional observers. `CommandEvidenceRecorder` records signed,
  owner- and run-scoped artifacts from the runner's own `SandboxResult`.
  `JsonArtifactVerifier` accepts an optional `EvidenceSigner`; with it, only authenticated
  evidence counts and the verdict is derived from recorded execution facts. Without it
  the verifier is unchanged.
- Evidence provenance is host-owned: `CommandEvidenceRecorder` records only for runners the
  host names in `trusted_runners`, never because a `SandboxResult` carries a fingerprint
  or image. The HMAC authenticates the artifact's origin and integrity, not the runner's
  honesty.
- After each run the runner compares the per-run copy with the snapshot digests;
  a run that modified or removed snapshot files cannot produce passing evidence. The
  per-run directory is owner-only.
- `testing_tools` can drop approval for `test`, `lint` and `typecheck` presets only when
  the host lists them explicitly and the runner declares real isolation.
  `commands.run_allowed` and `build` keep approval. No package-installation or
  deployment tool is added.

## Consequences

- Isolation is a risk-reduction boundary. A container escape, a vulnerable kernel, or a
  malicious Docker daemon defeats it; the daemon must be trusted. Docker socket access
  is host-equivalent authority and is never mounted.
- Dependencies installed outside the workspace snapshot, `node_modules`, and other
  excluded directories are absent from the sandbox, and the network is off by default;
  projects needing them require a prepared image. Evidence does not cover excluded paths.
- The evidence signer is a symmetric host secret; anyone holding it can forge evidence.
- Execution is not immutable. Same-user or root processes and a compromised daemon can
  alter the writable per-run copy; the post-run comparison detects most such changes but
  not a change that is restored before the run ends.
- Verified on Linux CI only. Windows Docker Desktop, macOS, rootless Docker, and Podman
  are not exercised or supported by this decision.
