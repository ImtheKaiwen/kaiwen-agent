# Releasing packages

The public source lives in one GitHub monorepo. Consumers install three separate
packages:

| Registry | Package | Purpose |
| --- | --- | --- |
| PyPI | `kaiwen-agent` | Trusted server runtime |
| npm | `@imthekaiwen/agent-protocol` | Shared contracts and schemas |
| npm | `@imthekaiwen/agent-ui` | Headless browser and React runtime |

## One-time registry setup

1. Verify that the `imthekaiwen` npm account can publish under its own scope.
2. Configure npm trusted publishing for this GitHub repository and the `npm`
   environment for both scoped packages.
3. Configure a PyPI pending trusted publisher for `kaiwen-agent`, using this
   repository, `.github/workflows/release.yml`, and the `pypi` environment.
4. In GitHub, create `npm` and `pypi` environments. Optional reviewer protection
   is recommended.

No long-lived npm or PyPI token is required after trusted publishing is enabled.

## Release checklist

1. Keep all three package versions aligned conceptually. Python alpha
   `0.1.0a1` corresponds to npm `0.1.0-alpha.1`.
2. Update changelog/release notes and run all local checks.
3. Confirm `npm run pack:check` and `python -m twine check dist/*`.
4. Commit and push the release state.
5. Run the `Release packages` GitHub workflow manually, selecting only the
   registries intended for that run.
6. Verify installation in clean Python and Node projects.
7. Create the matching GitHub release/tag only after registry verification.

Package versions are immutable. Never retry a partial release with altered files
under the same version; increment the prerelease version instead.
