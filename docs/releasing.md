# Releasing packages

The public source lives in one GitHub monorepo. Consumers install three separate
packages:

| Registry | Package | Purpose |
| --- | --- | --- |
| PyPI | `kaiwen-agent` | Trusted server runtime |
| npm | `@imthekaiwen/agent-protocol` | Shared contracts and schemas |
| npm | `@imthekaiwen/agent-ui` | Headless browser and React runtime |

## Versioning

The Python and npm packages are versioned independently. The only enforced
relationship is that `@imthekaiwen/agent-ui` depends on the exact version of
`@imthekaiwen/agent-protocol` that sits beside it in the workspace. Every release
also requires a `CHANGELOG.md` heading for the Python version. These rules are
checked by `python scripts/check_release.py metadata`.

Python versions use PEP 440 (for example `0.8.0b1`); npm versions use semver
(for example `0.2.0-alpha.1`). Record the matching npm versions in the release notes.
For registry releases, Git tags use `v<python version>` and are created manually
after registry verification.

The **first coding-team beta** (`v0.8.0b1`) was published separately as a
[GitHub Pre-release](https://github.com/ImtheKaiwen/kaiwen-agent/releases/tag/v0.8.0b1)
using CI-built wheel and sdist plus `SHA256SUMS`, without invoking the `release.yml` registry
publication workflow; the same two files were later published to PyPI by `publish-pypi-beta.yml` (below). See [`beta-release.md`](beta-release.md) for the completed record.
The process below governs registry package releases in general.

## Workflows

- `.github/workflows/verify.yml` is a reusable workflow containing every check:
  Ruff, mypy, pytest on Python 3.11, 3.12, and 3.13 (Linux) and 3.11 and 3.13
  (Windows), the executable examples, Node 20 and 22 build, protocol, test,
  typecheck, and `pack:check`, and a package job.
- The package job builds the wheel and sdist, runs `twine check --strict`, runs
  `scripts/check_release.py dist` (required resources including `py.typed` and the
  coding-kit prompts, and no monorepo content in the sdist), installs both artifacts
  into clean environments, and uploads them as the `python-dist` artifact. The Node
  job uploads the packed tarballs as `npm-packages`.
- `.github/workflows/ci.yml` calls `verify.yml` for pull requests and pushes to
  `main`, and exposes a single `CI gate` status for branch protection.
- `.github/workflows/release.yml` is manual (`workflow_dispatch`), runs only from
  `main`, calls `verify.yml`, and publishes the verified artifacts. It never rebuilds.
  Only the two publish jobs hold `id-token: write`, and each uses its GitHub
  environment (`npm`, `pypi`). All third-party Actions are pinned to full commit SHAs.

## PyPI publication of 0.8.0b1

`.github/workflows/publish-pypi-beta.yml` publishes exactly the wheel and sdist already attached to the
GitHub Pre-release `v0.8.0b1`; it builds nothing. It is manual (`workflow_dispatch`, no inputs) and runs only from
`main`. Its first job needs no OIDC token: it requires the tag to still point at the release commit
(`d8e10f8db90d14f5a6ca76ed5d95065e8acb8df6`), the release to be a published Pre-release, `0.8.0b1` to be absent
from PyPI, the downloaded files to match the SHA-256 values written in the workflow, the archive and metadata
checks and `twine check --strict`. Only the second job holds `id-token: write`; it runs in the `pypi`
environment, re-checks the hashes and uploads with the pinned `pypa/gh-action-pypi-publish`. There is no
`skip-existing`. Because `main` moves on, the standard `release.yml` is not used for this version: it would
rebuild from the current `main`.

Operator steps (PyPI account and repository settings, not done by any commit):

1. On PyPI, project `kaiwen-agent`, Publishing, add a GitHub trusted publisher: owner `ImtheKaiwen`,
   repository `kaiwen-agent`, workflow `publish-pypi-beta.yml`, environment `pypi`.
2. The existing publisher names the former repository `kaiwen-agent-platform` (workflow `release.yml`,
   environment `pypi`). It is not changed by this workflow. Once the new one works, consider removing it: a
   repository later created under the former name would otherwise match it.
3. In GitHub, give the `pypi` environment a required reviewer and a deployment branch policy limited to `main`
   (it currently has neither), then run the workflow from `main`.
4. Verify on PyPI, never by the workflow status alone: both files present with the recorded hashes, the Beta
   classifier, publisher provenance naming `ImtheKaiwen/kaiwen-agent` and `publish-pypi-beta.yml`, and a clean
   install from the public index.

## One-time registry setup

1. Verify that the `imthekaiwen` npm account can publish under its own scope.
2. PyPI trusted publisher for `kaiwen-agent`: owner `ImtheKaiwen`, repository
   `kaiwen-agent`, workflow `release.yml`, environment `pypi`.
3. npm trusted publisher for each scoped package: the same owner, repository
   `kaiwen-agent`, workflow `release.yml`, environment `npm`. Trusted publishing
   needs npm 11.5.1 or newer, which the workflow installs at that exact version.
4. If a trusted publisher was registered under the former repository name
   `kaiwen-agent-platform`, update it. A mismatch makes the OIDC exchange fail.

No long-lived npm or PyPI token is required after trusted publishing is enabled.

## Recommended GitHub settings

These are repository settings and are not changed by any workflow or commit.

- Ruleset on the default branch: require a pull request, require the `CI gate` status
  check with branches up to date, block force pushes and deletion, require linear
  history, and allow only squash merging.
- Environments `npm` and `pypi`: required reviewers, and a deployment branch policy
  limited to `main`.
- Actions: enable "Require actions to be pinned to a full-length commit SHA".
- Keep default workflow permissions read-only (the current value).
- Review pinned Action SHAs when updating; Dependabot for `github-actions` can propose
  updates if desired.

## Release checklist

1. Update the version and `CHANGELOG.md`, then merge through a pull request whose
   `CI gate` passes.
2. Run the `Release packages` workflow from `main`, selecting only the intended
   registries. Verification runs again and must pass before any publish job starts.
3. Verify installation in clean Python and Node projects.
4. Create the GitHub release and `v<python version>` tag only after registry
   verification.

Package versions are immutable. Never retry a partial release with altered files
under the same version; increment the prerelease version instead.
