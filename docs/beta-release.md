# First beta: release preparation

Status: prepared, not published. Nothing in this document has been released, tagged or uploaded. The actual
release needs a separate authorization.

## Proposed version and channel

- Python version: `0.8.0b1` (PEP 440; the previous tag is `v0.7.0a1`, and `scripts/check_release.py` accepts
  `a`, `b` and `rc` suffixes). Git tag: `v0.8.0b1`, created manually after verification, as in
  [`releasing.md`](releasing.md).
- Channel: a GitHub Pre-release of the repository, with the wheel, the sdist and a `SHA256SUMS` file
  attached. Not PyPI and not npm: the `Release packages` workflow publishes to registries and is not part of
  this beta. The npm packages (`@imthekaiwen/agent-protocol`, `@imthekaiwen/agent-ui`) are not part of the
  coding-team beta and are not released with it.
- The version is not bumped in the repository yet. Bumping it and renaming the `Unreleased` heading of
  `CHANGELOG.md` to `0.8.0b1` is step 1 of the checklist, in its own pull request.

## Supported capabilities (first beta)

- A runnable, credential-free coding team (Lead, Coder, Tester, Reviewer) over fenced SQLite execution, with
  a real completion gate and host-controlled team acceptance; ten offline scenarios and five Docker scenarios
  whose outcomes come from platform records.
- A Tester that runs a registered command only inside a digest-pinned, network-less, non-root, read-only
  container; signed, run-scoped evidence; no fallback to local execution.
- Deny-by-default tool permissions per role; cancellation and fenced ownership.
- An authorization-gated maintainer harness for a controlled real-provider validation
  (`scripts/live_team_validation.py`). It is not part of the installed package and is not for beta users.
- The general package surface described in the README (provider-neutral runtime, tools, policy, persistence).

## Known limitations (kept visible)

- Live evidence is one passing run of the accepted scenario (one model, one small project, an earlier commit);
  later corrections were verified offline and by CI only. It does not show reliability or model quality.
- No author-independent review has taken place; every review so far was by the same model family as the author.
- The default example is scripted. A model-driven team needs host code and, for the Tester, Docker.
- Not implemented: approval-required delegation, a first-party approval store, general resume, model-driven
  file edits with approvals inside a team, multi-process execution, a unified operator view across roles.
- The example is not part of the wheel; first-beta use is from a source checkout (see
  [`quickstart-coding-team.md`](quickstart-coding-team.md)).
- Commit pinning of the paid validation harness is guaranteed for its command line only.
- Not verified: macOS, Podman, Windows containers, and a Linux workstation install by the quick-start commands.
- Not production ready.

## Release notes (draft for the GitHub Pre-release)

> **Kaiwen Agent 0.8.0b1 (beta)**. A preview for evaluation, not for production. It adds a runnable
> coding-team example (Lead, Coder, Tester, Reviewer) whose outcomes are decided by the host's completion gate
> and team acceptance, a Docker-isolated Tester, and a maintainer-only live-validation harness. Install the
> attached wheel into a Python 3.11+ virtual environment and follow `docs/quickstart-coding-team.md` from a
> source checkout of the same tag. Verify the files against `SHA256SUMS`. Known limitations are listed in
> `docs/beta-release.md`. No production-readiness claim is made.

## Artifact verification (rehearsal, 2026-11-18)

A throwaway bump of the version to `0.8.0b1` (reverted, not committed) was built from this tree to prove the
release path before authorization: `python scripts/check_release.py metadata` OK; `python -m build` produced
`kaiwen_agent-0.8.0b1-py3-none-any.whl` and `kaiwen_agent-0.8.0b1.tar.gz`; `twine check --strict` PASSED for both;
`python scripts/check_release.py dist <dir>` OK. These hashes are for the rehearsal only; the release artifacts are
rebuilt by CI from the release commit and their hashes are the ones that count.

For the currently committed version `0.7.0a1` the wheel and sdist were installed into clean virtual environments
and the example was run (see the quick start for what was verified and what was not).

## Release checklist

Each step after 1 needs the release authorization.

1. Pull request: set `version = "0.8.0b1"` in `pyproject.toml`, rename `## Unreleased` in `CHANGELOG.md` to
   `## 0.8.0b1`, run `python scripts/check_release.py metadata`, merge once the `CI gate` passes.
2. From the merge commit on `main`, take the `python-dist` artifact of its CI run (the package job builds it,
   runs `twine check --strict` and `check_release.py dist`, and installs both files into clean environments).
   Do not rebuild locally for the release.
3. Verify: the wheel and sdist names carry `0.8.0b1`; compute `sha256sum` and write `SHA256SUMS`; install the
   wheel into a clean Python 3.11+ virtual environment; `pip check`; run
   `examples/coding-team-acceptance/main.py` from a checkout of the tag; run the `--docker` scenarios where an
   engine is available.
4. Create the tag `v0.8.0b1` on that commit and a GitHub Pre-release (the pre-release flag set) with the two
   files, `SHA256SUMS` and the release notes above.
5. Re-run the quick start from the published release in a clean environment; record the result.
6. Versions are immutable: if anything is wrong after publishing, release `0.8.0b2`; never replace files.
