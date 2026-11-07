# Release readiness

> Historical snapshot: this document records the state at the time it was written
> and is not updated as the code evolves. Current status is in `docs/roadmap.md`.

## Decision

**Release complete.** There are no known critical or high-severity runtime failures,
the bounded live OpenAI check passes, and release workflow run `39000220914` passed
verification before publishing both npm packages. PyPI `kaiwen-agent 0.5.0a1`, npm
`@imthekaiwen/agent-protocol 0.2.0-alpha.1`, npm `@imthekaiwen/agent-ui
0.2.0-alpha.1`, and GitHub prerelease `v0.5.0a1` are published. Clean-environment
Python and TypeScript consumer smoke tests passed against the registry artifacts.

## Remaining non-blocking follow-up

1. Review the low-severity development-only `esbuild` advisory. A forced upgrade is
   not recommended until the project raises its Node support floor enough for the
   fixed `tsup` major, or an upstream compatible release becomes available.

Gemini, Ollama, PostgreSQL, Redis, real media, and telephony provider checks are
optional-provider validation gaps. They block claims about those deployments, but do
not block an alpha release if release notes state the limitation.

## Release artifacts

- PyPI: `kaiwen-agent`
- npm: `@imthekaiwen/agent-protocol`
- npm: `@imthekaiwen/agent-ui`

The repository's `release.yml` verifies Python and Node checks before publishing and
uses PyPI trusted publishing plus npm provenance. The workflow now also runs formatter
checks and the isolated Todo/legal validations.

## Post-publish verification

Completed:

1. installed exact PyPI `0.5.0a1` in a new virtual environment;
2. imported the public `AgentRuntime` API and ran `pip check`;
3. installed both exact npm `0.2.0-alpha.1` packages in a clean consumer project;
4. built the strict TypeScript consumer and executed its generated JavaScript;
5. confirmed both npm packages expose `alpha` as `0.2.0-alpha.1` while preserving
   the prior `latest` tag;
6. confirmed PyPI, npm, Git tag, GitHub Release, and changelog versions align.
