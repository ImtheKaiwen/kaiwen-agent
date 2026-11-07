# Permissions and sandbox boundary

Tool bundles are names, not grants. Effective authority is the intersection of the
agent definition, capability manifest, workspace profile, trusted identity, policy,
approval state and host runtime controls.

- Inspection requires `workspace.read` or `git.read`.
- Patches and directory creation require `workspace.write` and approval.
- Command tools accept only host-authored presets and whole-token parameters.
- Command execution requires an injected `SandboxRunner`; the default unsupported
  runner fails closed.
- Reviewers are read-only by default. Tester or DevOps writes need explicit grants.
- A denied, expired or unrelated approval never authorizes another call.
- Workspace roots and tenant/user identities come from trusted host state.

Never implement a convenience fallback using `shell=True`, concatenate model text
into commands, forward `.env` content, or treat a selected bundle as permission.

## First-party Docker sandbox (M6.2)

`DockerSandboxRunner` implements the existing `SandboxRunner` protocol for hosts that
have a trusted Linux Docker engine. It is optional; the default remains the fail-closed
`UnsupportedSandboxRunner`. See ADR 0012 and `examples/coding-docker-evidence`.

Trusted host configuration (`DockerSandboxConfig`): a digest-pinned image
(`name@sha256:...`, enforced unless `require_digest=False` for local development), the
Docker client path, a non-root numeric `user`, memory, CPU, process, and `/tmp` size
limits, an optional network opt-in, and a small static environment. The model selects
only a host-registered command preset; it never reaches the image, a flag, a mount, or a
host path.

Enforced for every run:

- explicit `docker run` argv, no host shell, `--pull=never` (the image must already be
  present and, when pinned, match its digest; the runner never pulls);
- `--network none` unless the host sets `allow_network=True` and the preset asks for it;
- non-root user (root is rejected at configuration time), `--cap-drop=ALL`,
  `--security-opt=no-new-privileges`, `--read-only` root filesystem, a bounded
  `nosuid,nodev` tmpfs at `/tmp`, `--pids-limit`, `--memory` (swap equals memory),
  `--cpus`, `core=0`, no container log retention;
- no privileged mode, host namespaces, devices, added capabilities, Docker socket, or
  host-path mounts other than one private per-run copy of the execution snapshot;
- no host environment forwarded to the container; the Docker client receives only an
  allowlist of its own variables (for example `PATH`, `DOCKER_HOST`);
- wall-clock timeout, bounded stdout and stderr (runaway output stops the container),
  and removal of the container and temporary files after success, failure, timeout, or
  cancellation;
- model-facing errors are generic; engine details are logged server-side only.

Anything that cannot be verified (engine unreachable, not Linux, image missing or not
matching its digest, snapshot rejected) raises `SandboxUnavailableError`. There is no
fallback to host execution.

### Execution snapshots and fingerprints

Commands never see the live workspace. `SnapshotBuilder` copies the files the workspace
read policy allows into a private directory, reading each file once from an open
descriptor and hashing the bytes it copies. Protected paths (`.git`, secrets, dependency
and build directories), symbolic links (rejected by default), special files, and
over-limit workspaces are excluded or rejected; a partial snapshot is never produced.
Each run executes in a fresh copy of the snapshot, re-verified against its digests, so
tests cannot change the snapshot or the host workspace.

`ExecutionSnapshot.fingerprint` covers every copied file's relative path, size,
content digest, and executable bit. It is distinct from `ProjectDiscovery.fingerprint`,
which covers file paths and manifest content only and is not suitable as evidence of
project state. Changes inside excluded directories do not change the fingerprint.
`DockerSandboxRunner.bound_to(snapshot)` pins execution to a captured snapshot, so
evidence names the exact state that ran; an unbound runner captures the current
workspace for each run.

### Evidence

Register `CommandEvidenceRecorder` as an observer of `CommandCatalog`, naming the
runners the host trusts (`trusted_runners`, required) and `EvidenceBinding`s that map a
preset to a requirement and evidence type. After a real run it records an owner- and
run-scoped artifact containing the preset, validated parameters, snapshot fingerprint,
image identity, exit code, outcome, duration, snapshot integrity, checksums, and bounded
redacted output, signed with a host-held `EvidenceSigner`. Pass the same signer to
`install_coding_verifiers(..., signer=...)`: the command verifiers (`coding.tests`,
`coding.acceptance`) then accept only signed evidence and derive the verdict from the
recorded exit code and flags. Failure, timeout, cancellation, missing evidence, a
different run or owner, a different snapshot fingerprint, or a run that changed its own
snapshot files never pass. The run id comes from the trusted `runtime_run_id` in the
agent context metadata. Without a signer the verifiers keep their earlier behavior and
trust host-written JSON artifacts.

Provenance. Fields of a `SandboxResult` (fingerprint, image, exit code) can be filled in
by any runner, including `FakeSandboxRunner`, so they are not provenance. The recorder
records only when the runner object configured on the `CommandCatalog` is one the host
named in `trusted_runners` (or a runner sharing a trusted `DockerSandboxRunner`'s
`provenance_domain`, which includes runners from `bound_to`). A fake or unnamed runner
produces no evidence even with fully populated result fields. A custom runner is
supported by passing it explicitly; that is a host trust decision, and the platform
cannot check that such a runner really isolates or really executed anything.

What the HMAC proves: the artifact content and its labels were produced by a holder of
the signing key and have not been altered since, in this tenant, user, and run. It does
not prove that a command ran in a sandbox, that the runner is honest, or that the key was
kept secret. Those remain the trusted host's responsibility: choose the trusted runners,
keep the Docker daemon and client trusted, protect the key (anyone holding it can forge
evidence), and give the model no way to write signed artifacts.

### Execution integrity

Each run executes in a fresh per-run copy of the snapshot, verified against its digests
when it is created. That copy is writable by the container user (so tests can write
output), which means it is not immutable:

- The per-run directory is owner-only (`0700`), so another local user cannot reach the
  world-writable copy inside it. Earlier revisions created it `0711`, which allowed any
  local user who learned the path to modify the files between verification and
  container start; this is fixed.
- After the container has stopped, the runner re-hashes every snapshot file in the copy.
  Any snapshot file that was changed, deleted, or replaced (for example by a symbolic
  link) sets `snapshot_unchanged=False` and lists `modified_paths`; new files, such as
  caches and test output, are allowed. A result that is not verified unchanged never
  passes the gate, so a command cannot rewrite its own sources or tests and still earn
  evidence for the original fingerprint.
- This is a post-run comparison, not immutability. A process running as the same host
  user or as root, or a compromised Docker daemon, can still change bytes during a run,
  including changing and restoring them within one run. Commands that legitimately
  rewrite tracked files (formatters with write mode) will fail this check by design.

### Unattended test, lint and typecheck

`testing_tools(catalog, unattended_categories=frozenset({"test", "lint", "typecheck"}))`
removes the per-call approval for those categories only, and only when the catalog's
runner declares real isolation. `build` and `commands.run_allowed` always keep their
approval requirement. Policy, permissions, audit, and idempotency are unchanged.

### Platform status

Exercised: Linux (CI ubuntu runners with a real Docker engine). Not exercised:
Windows with Docker Desktop and WSL2, macOS, rootless Docker, Podman. Windows hosts
need Docker Desktop in Linux-container mode with the temp directory on a shared drive.
Containers reduce risk; they are not a guarantee against container escape, a
vulnerable kernel, or a malicious or compromised Docker daemon. Anyone able to talk to
the Docker daemon controls the host, so the daemon and client must be trusted.
