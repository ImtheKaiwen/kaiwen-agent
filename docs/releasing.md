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

Python versions use PEP 440 (`0.7.0a1`); npm versions use semver (`0.2.0-alpha.1`).
Record the matching npm versions in the release notes. Git tags use `v<python
version>` and are created manually, after registry verification.

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
