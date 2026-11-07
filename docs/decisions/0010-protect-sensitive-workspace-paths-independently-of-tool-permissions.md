# ADR 0010: Protect sensitive workspace paths independently of tool permissions

- Status: Accepted

## Context

`WorkspaceFilesystem` applied its ignore and sensitive-path policy to listing, stat,
and line reads, but `read_text` and `write_text` did not. The `workspace.read_text`,
`workspace.write_text`, and `workspace.apply_patch` tools therefore could read `.env`
files, private keys, and Git internals, or write to `.git/hooks`, with only the
ordinary `workspace.read` or `workspace.write` permission. Ignored-name matching was
case-sensitive and did not account for Windows aliases. The read-only Git tools could
read protected blobs with `rev:path` revisions and printed protected files in diffs.

## Decision

- Protected paths are denied for every filesystem operation, read and write. A
  `workspace.read`, `workspace.write`, or `git.read` grant never authorizes exposing a
  secret value to a model or external provider.
- The policy has no per-call or per-tool override. `.git` is always protected, even
  when a host supplies its own ignore list. Sensitive names are fixed in code.
- Name matching folds case, NTFS stream suffixes, and trailing dots or spaces.
- Git read tools reject `rev:path` revisions and apply the same path policy to the
  `path` argument. Diff, show, and status exclude protected paths by pathspec, derived
  from the filesystem's effective policy: sensitive names plus every configured ignored
  name, matched at any depth and case-insensitively. Model-facing Git tools therefore
  neither print protected contents nor enumerate protected paths. Names that cannot be
  expressed as a safe pathspec make the Git tools fail closed.
- A future explicit, host-owned grant for a named protected file would require a new
  decision record and an audit event.

## Consequences

- Tools can no longer read or write `build`, `dist`, `node_modules`, and similar
  ignored directories, which were already hidden from listing and line reads.
- The policy is path-based. Secret values inside ordinary files are not detected.
- Git history is filtered by current path only. A protected file that was renamed to
  an ordinary name appears under that name in historical diffs, and `git.log` shows
  commit metadata unfiltered.
