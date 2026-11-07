# CLAUDE.md

Session rules for Claude Code. Project boundaries, invariants, and verification
commands live in `AGENTS.md` and are not repeated here.

## Read first

- Read `AGENTS.md` before modifying the project.
- `docs/roadmap.md` is the authoritative milestone tracker.
- Before changing an interface or a package boundary, read the relevant part of
  `docs/architecture.md` and the accepted records in `docs/decisions/`.
- Existing audit findings are unverified until independently reproduced.

## Scope and approval

- Work only on the approved milestone. Ask for explicit approval before starting a
  new milestone or expanding scope.
- Execute approved milestones end-to-end, including implementation, verification,
  documentation, commit, and review preparation. Avoid unnecessary approval requests
  for routine steps; stop for genuine blockers, destructive operations,
  security-sensitive changes, or scope expansion.
- Never publish packages, push tags, or merge pull requests without explicit
  authorization. Do not commit or push unless asked.

## Documentation

- Use technical English. Do not use emojis in repository Markdown.
- Update only the documentation affected by the change. Do not add Markdown files
  unless a decision record or subsystem document is required; avoid duplicating
  existing text.
- Record architectural decisions as ADRs in `docs/decisions/`.
- Keep `docs/roadmap.md` synchronized with verified progress only.
- Update `CHANGELOG.md` only for meaningful user-visible changes.
- Dated validation reports are historical snapshots; do not rewrite them to look
  current.

## Verification integrity

- Never claim tests, linting, or builds passed without execution evidence from the
  current session; state what was not run.
- Never weaken tests, assertions, validation, or security checks to obtain a
  passing result.
- Never silently revert temporary test-related changes to conceal a failure; report
  the failure.
