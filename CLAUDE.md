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

## Controlled autonomous development

An operator-approved milestone is delivered end-to-end without repeated prompts.

- Routine coding, tests, compatible refactoring, and documentation inside the approved
  milestone do not need separate approval. Diagnose and fix ordinary failures yourself.
- A milestone authorization may explicitly include normal commits, non-force pushes to its
  dedicated feature branch, and creating or updating its Draft pull request. Without that
  explicit statement, do not commit or push.
- A new milestone, or significant scope expansion beyond the approved package, still needs
  authorization. So does any security architecture change beyond the approved design,
  breaking a public interface, or an irreversible or destructive migration.
- Merge, tags, releases, publishing, production deployment, destructive operations,
  force-push, and paid API usage always need explicit approval, whatever the milestone says.
- Independent review is not replaced by self-reported CI success. A Draft pull request ends
  the milestone; do not mark it ready or merge it.
- Work in an isolated worktree and branch from verified main. Preserve other agents'
  branches, worktrees, and uncommitted changes; never overwrite or rebase another pull
  request's branch.
- Keep progress recoverable across interrupted sessions: commit coherent steps on the
  feature branch, keep `docs/roadmap.md` limited to verified facts, and leave the branch in a
  state a new session can resume from by reading the repository.
- Stop and report with evidence when a required guarantee cannot be established, rather than
  forcing a green result. `AGENTS.md` remains authoritative for repository invariants and
  verification commands.

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
