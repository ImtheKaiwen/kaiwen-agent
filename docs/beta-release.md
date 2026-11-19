# First beta: published release and verification

Status: **published and verified** as GitHub Pre-release
[`v0.8.0b1`](https://github.com/ImtheKaiwen/kaiwen-agent/releases/tag/v0.8.0b1)
(2026-11-18). This document preserves the release plan, execution record and beta limitations.
This is not a stable or production release.

## Version and channel

- Python version: `0.8.0b1` (PEP 440; the previous tag is `v0.7.0a1`, and `scripts/check_release.py` accepts
  `a`, `b` and `rc` suffixes). Git tag: `v0.8.0b1`, created manually after verification, as in
  [`releasing.md`](releasing.md).
- Channel: a GitHub Pre-release of the repository, with the wheel, the sdist and a `SHA256SUMS` file
  attached, and afterwards the same two files on PyPI (see the PyPI section below). Not npm. The npm packages (`@imthekaiwen/agent-protocol`, `@imthekaiwen/agent-ui`) are not part of the
  coding-team beta and are not released with it.
- The version bump, Beta classifier and changelog heading were completed in pull request #26,
  squash-merged as `d8e10f8db90d14f5a6ca76ed5d95065e8acb8df6`.

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

## Release notes (text prepared for the GitHub Pre-release)

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

For the then-current version `0.7.0a1`, the wheel and sdist were installed into clean virtual environments
and the example was run before the `0.8.0b1` release (see the quick start for verification scope).

## Published artifact verification (2026-11-18)

- Release commit and lightweight tag `v0.8.0b1`: `d8e10f8db90d14f5a6ca76ed5d95065e8acb8df6`.
- Post-merge `main` CI run `44362468039`: all 10 jobs passed. The wheel and sdist were taken
  unchanged from that run's `python-dist` artifact, not rebuilt locally for the release.
- Published `kaiwen_agent-0.8.0b1-py3-none-any.whl` SHA-256:
  `3bd59e28a653416dc4bc55b5892cf41e56bf809bebc9112abe43b896883d063a`.
- Published `kaiwen_agent-0.8.0b1.tar.gz` SHA-256:
  `1d83404090f9aa5897732a145b65c3ac793a03b33423f8ce563542724d426679`.
- Published assets include `SHA256SUMS`; downloaded assets matched it and the CI artifacts byte-for-byte.
  The published wheel installed into a fresh Python 3.13 environment (`pip check` clean),
  and the example from the release tag passed offline and in five Docker scenarios.
  The published-wheel install was not repeated on Python 3.11 (pre-release wheel testing was).
- No PyPI or npm publication occurred at the time of the GitHub release. These checks do not establish production readiness.

## PyPI publication (2026-11-19)

Published from the `publish-pypi-beta.yml` workflow, run 44472844371 on `main` at
`81a93b7b3699abf6b69b9c08cffca3112603e00f`, uploading the unmodified release files after it re-verified the tag,
the pinned hashes and the metadata. Independently verified afterwards: PyPI serves `kaiwen-agent` `0.8.0b1`
(`Requires-Python >=3.11`, `Development Status :: 4 - Beta`) with the wheel (`3bd59e28...063a`) and the sdist
(`1d834040...6679`) whose SHA-256 values equal the GitHub release files; the attestations name the publisher
`ImtheKaiwen/kaiwen-agent`, workflow `publish-pypi-beta.yml`, environment `pypi`; and
`pip install --no-cache-dir "kaiwen-agent==0.8.0b1"` into fresh Python 3.11 and 3.13 environments passed
`pip check` and an import check, and the example from the release tag passed offline and in five Docker scenarios.
The GitHub release stays a Pre-release. This does not establish production readiness.

## Release checklist (completed historical procedure)

1. Pull request: set `version = "0.8.0b1"` and the `4 - Beta` classifier in `pyproject.toml`, rename `## Unreleased`
   in `CHANGELOG.md` to `## 0.8.0b1`, run `python scripts/check_release.py metadata`, merge once the exact head passes
   every check, then confirm the post-merge CI of `main` (the release commit).
2. Before the tag exists: confirm the tag is absent; take the `python-dist` artifact of the CI run of the release
   commit (the package job builds it, runs `twine check --strict` and `check_release.py dist`, and installs both files
   into clean environments). Do not rebuild locally for the release.
3. Verify from the exact release commit: the file names carry `0.8.0b1`; metadata and archive integrity; install the
   wheel into a clean Python 3.11+ virtual environment; `pip check`; run
   `examples/coding-team-acceptance/main.py` from a checkout of the release commit; run the `--docker` scenarios where
   an engine is available; compute `sha256sum` and write `SHA256SUMS`.
4. Create the tag `v0.8.0b1` on that commit and a GitHub Pre-release (pre-release flag set) with the two files,
   `SHA256SUMS` and the release notes above.
5. After the tag exists: download the published files and compare them with `SHA256SUMS`; install the published wheel
   into a clean environment and run the quick start from a checkout of the tag; record the result.
6. Versions are immutable: if anything is wrong after publishing, report it and release `0.8.0b2`; never replace
   or overwrite published files or the tag.
