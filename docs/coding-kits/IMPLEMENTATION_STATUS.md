# Coding Kits implementation status

> Historical snapshot: this document records the state at the time it was written
> and is not updated as the code evolves. Current status is in `docs/roadmap.md`.
> The plan file it references, `KAIWEN_AGENT_CODING_KITS_IMPLEMENTATION_PLAN.md`, is
> not present in this repository.

This table records the evidence-backed baseline and incremental implementation of
`KAIWEN_AGENT_CODING_KITS_IMPLEMENTATION_PLAN.md`. The checked-out code remains the
authority when the plan and implementation differ.

Baseline commit: `4706b8961e5c001b081484cfa43f92d9a520af64`
Audit date: 2026-10-17

## Baseline verification

| Check | Result | Evidence |
|---|---|---|
| Python tests | pass | `230 passed, 1 skipped`; `python -m pytest backend/tests` |
| Python lint | pass | `python -m ruff check backend` |
| Python types | pass | `python -m mypy --no-incremental backend/src` |
| TypeScript build | pass | `npm run build` |
| TypeScript types | pass | `npm run typecheck` |
| TypeScript tests | pass | protocol 2 tests and UI 7 tests via `npm test` |

The skipped Python test is the existing optional PostgreSQL integration test and is
not treated as a Coding Kits success.

## Architecture inventory

| Capability | Baseline | Reuse or extension | Evidence / test reference |
|---|---|---|---|
| Single-agent model/tool loop | exists | reuse unchanged | `agent.py`, `runtime/agent_runtime.py` |
| Definition/profile composition | exists | reuse | `runtime/factory.py`, `runtime/profile.py` |
| Provider registry and technical capabilities | exists | reuse; add kit preflight later | `models/registry.py`, `models/capabilities.py` |
| Central tool security pipeline | exists | reuse for every toolkit definition | `tools/execution.py` |
| Root-bounded text read/write | exists | conservatively extend | `integrations/filesystem.py`, `test_integrations.py` |
| Generic list/search/stat/ranged-read tools | implemented | CK1; reuse `WorkspaceFilesystem` | `test_toolkits_readonly.py` |
| Read-only Git tools | implemented | CK1 fixed shell-free adapter | `test_toolkits_readonly.py` |
| Safe patch and command catalog | implemented | CK2; host sandbox adapter required for real processes | `test_toolkits_write_and_commands.py` |
| Prompt registry | exists | install packaged English coding prompts | `prompts/registry.py` |
| Standards resolver | exists | install coding packs | `standards/` |
| Verifier registry and completion gate | exists | reused by coding JSON evidence verifiers | `test_coding_standards.py` |
| Durable workflow controller | exists | reuse; add planning/revision adapters | `workflow/` |
| Optional team delegation | exists | reuse; add registered role presets | `teams/`, `jobs/delegation.py` |
| Context, compaction, artifacts | exists | reuse and observe | `context_engine/`, `artifacts/` |
| Durable runs, approvals, receipts | exists | reuse; no kit-owned recovery engine | `runtime/durability.py`, `tools/durability.py` |
| First-party Coding Kit manifest | partial | Generic immutable kit registry and coding prompt layer exist; full manifest follows standards/profiles | `test_coding_prompts.py` |
| Project discovery and documentation maintenance | implemented | Bounded provenance snapshot, checksum invalidation, proportionate create/update proposals | `test_coding_discovery_documentation.py` |
| `CodingAgent` public composer | implemented | Opt-in facade over `AgentFactory`/`AgentRuntime`; workflow execution delegates to `WorkflowController` | `test_coding_agent.py` |
| `CodingTeam` public composer | implemented | Adaptive exact role definitions; execution stays in `TeamRuntime`/`JobManager` | `test_coding_team.py` |
| Provider compatibility preflight | implemented | Host-registered factories/capabilities, strict locality/tool/structured-output checks | `test_coding_providers.py` |
| Coding observability | implemented | Coding observations mapped into owner-scoped canonical events; durable manifest artifact | `test_coding_observability.py` |

## Phase status

| Phase | Status | Notes |
|---|---|---|
| CK0 | implemented | Audit complete; package boundary accepted in ADR 0008; baseline green. |
| CK1 | implemented | Bounded filesystem/search/Git/project adapters and non-authorizing bundles. |
| CK2 | implemented | Checksum patch/mkdir plus fixed command catalog; fake and fail-closed sandbox ports. No built-in real runner is claimed. |
| CK3 | implemented | Eleven packaged English prompt definitions, base/role composition, compact variant, typed Turkish preference, conflict-safe installer. |
| CK4 | implemented | Eleven conditional/proportionate packs and owner/run/fingerprint-scoped JSON evidence verifiers. |
| CK5 | implemented | Bounded discovery, provenance fingerprints, owner-scoped snapshot artifacts, tiered non-overwriting documentation proposals. |
| CK6 | implemented | Single-agent composer, three profiles/modes, bounded objective planner/revision adapter, existing controller entry point. |
| CK7 | implemented | Adaptive least-privilege role composition; optional roles from trusted facts; writes serialize unless host isolation exists. |
| CK8 | implemented | Host-owned provider registration, truthful tool/structured/locality preflight, unknown-safe usage DTO. |
| CK9 | implemented | Durable run-manifest artifacts and canonical event mapping; existing recovery/compaction stores remain authoritative. |
| CK10 | implemented | Quickstarts/safety/customization/troubleshooting, wheel resources, clean install and readiness report. No publication. |

## Adaptations from the plan

- The implementation uses an opt-in subtree in the existing Python distribution,
  rather than creating a second package immediately. This avoids duplicated release
  compatibility while preserving lazy imports and a lightweight core.
- `WorkspaceProfile` remains strict and unchanged; future kit configuration compiles
  into it instead of adding coding-specific fields.
- Existing registries reject duplicate registrations. A kit installer must first
  compare exact immutable definitions/checksums and be idempotent only for an
  identical installed definition; it must reject conflicts.
- `WorkflowController` consumes validated `WorkflowPlan` objects. Natural-language
  planning belongs in a bounded adapter and never becomes another execution loop.
