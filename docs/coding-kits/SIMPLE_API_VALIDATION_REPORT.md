# Simple Coding API validation report

> Historical snapshot: this document records the state at the time it was written
> and is not updated as the code evolves. Current status is in `docs/roadmap.md`.

Status: deterministic release-readiness validation complete; no package has been
published.

## Verified deterministic behavior

| Area | Evidence | Result |
| --- | --- | --- |
| Existing backend baseline | `python -m pytest backend/tests -q` before implementation: 273 passed, 1 skipped | Pass |
| Lazy local chat | `test_local_chat_is_lazy_tool_free_and_preserves_turkish_preference` | Pass |
| Explicit action boundary | `test_action_requires_exact_trusted_allowlist_and_never_leaks_tools_to_chat` | Pass |
| Approval/capability preflight | `test_action_preflight_requires_real_approval_gate_and_capabilities` | Pass |
| Locality fail-closed | `test_local_only_fails_closed_for_cloud_model` | Pass |
| Workspace is not authority | `test_workspace_path_does_not_grant_action_authority` | Pass |
| Real sandbox claim | `test_workflow_requires_real_sandbox_when_host_marks_it_required` | Pass |
| Existing durable workflow | `test_workflow_handle_uses_durable_run_and_rejects_cross_owner_resume` | Pass |
| Truthful lifecycle support | `test_pause_and_cancel_are_explicitly_unsupported_without_host_control` | Pass |
| Static analysis | Ruff and strict mypy on the coding package | Pass |
| Full Python suite | `python -m pytest`: 282 passed, 1 optional PostgreSQL test skipped | Pass |
| Full Python quality | Ruff format/check and strict mypy over 241 source files | Pass |
| Node workspaces | build, protocol schema check, 9 tests, typecheck, dry-run packs | Pass |
| Python distributions | isolated Hatch wheel/sdist build and Twine check | Pass |
| Installed consumer | clean venv, built wheel, chat and durable workflow examples | Pass |
| Package hygiene | wheel/sdist scan for `.env`, key, PEM and bytecode files | Pass |
| OpenAI live chat | One minimal tool-free `CodingKit.local` call; text and usage returned | Pass |

The workflow test uses the real `ObjectivePlanner`, `WorkflowController`,
`TaskScheduler`, `CompletionGate`, `DurableRunManager`, SQLite adapter and
`FunctionWorker`. It does not use a facade-owned task engine.

## Not yet claimed

- Gemini live connectivity was not exercised in this validation run.
- Ollama connectivity depends on a separately installed server and model.
- A production sandbox is host infrastructure and is not bundled.
- Distributed team execution requires a concrete host launcher backed by the existing
  `TeamRuntime`/`JobManager` boundary.
- No paid-provider performance comparison was run. The minimal OpenAI smoke proves
  connectivity, not a performance baseline. Deterministic legacy and facade
  suites share the same runtime path; timing a fake provider would not establish live
  provider performance.

## Release assessment

The additive API introduces no mandatory dependency and preserves the existing direct
factory APIs. The prepared prerelease candidate is `0.7.0a1`, because the facade is a
new public feature. Tagging and publishing require owner approval and were deliberately
not performed.

No secret values are printed or persisted by these checks.
