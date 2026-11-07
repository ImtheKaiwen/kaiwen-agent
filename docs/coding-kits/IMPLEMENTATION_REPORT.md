# Coding Kits implementation report

> Historical snapshot: this document records the state at the time it was written
> and is not updated as the code evolves. Current status is in `docs/roadmap.md`.

## Baseline and architecture

- Baseline commit: `4706b8961e5c001b081484cfa43f92d9a520af64`.
- Decision: ADR 0008 keeps Coding Kits as opt-in `kaiwen_agent.kits` and
  `kaiwen_agent.toolkits` modules in the existing distribution.
- Reused without replacement: `AgentRuntime`, `AgentFactory`, `ToolExecutor`, policy
  and approvals, `WorkflowController`, `TaskScheduler`, `TeamRuntime`, context and
  compaction, durable runs/actions/receipts, artifacts, standards and completion gate.
- Root `kaiwen_agent` imports remain independent from Coding Kits.

## Implemented public APIs

- `CodingKitFactory`, `CodingKitConfig`, `CodingAgent`.
- `ObjectivePlanner` and `BoundedCodingRevisionPlanner` producing validated
  `WorkflowPlan` values; neither executes model-authored JSON directly.
- `CodingTeamFactory`, role configuration and trusted-fact adaptive role selection.
- `ProjectDiscovery`, content fingerprinting and proportionate documentation proposals.
- `CodingProviderCatalog` and model tool/structured/locality compatibility preflight.
- Safe filesystem/search/Git/patch/project/command/testing tool definitions.
- Coding run manifest artifacts and canonical owner-scoped event mapping.

## Prompts

All bundled prompts are English, immutable version `1` resources. IDs:

`coding.base`, `coding.solo`, `coding.lead`, `coding.planner`, `coding.architect`,
`coding.designer`, `coding.coder`, `coding.tester`, `coding.reviewer`,
`coding.devops`, `coding.compact`.

Exact SHA-256 checksums are recorded in `PROMPTS.md`. User-facing Turkish is a typed
conversation preference and was live-smoked separately from system prompt language.

## Standards and evidence

Eleven versioned packs cover foundation, documentation, tests, architecture,
security, performance, CI, data, API, accessibility and minimal-neutral UI. Trusted
condition facts determine applicability; an arbitrary expression evaluator was not
introduced. JSON artifact verifiers require exact owner, run, fingerprint,
requirement and evidence type. Missing, malformed or stale evidence is inconclusive;
failed mandatory evidence blocks completion.

## Tools, approvals and sandbox

- Read tools are root-bound, bounded and secret/vendor-aware.
- Git operations are fixed read-only subprocess invocations without a shell.
- Patch writes require exact preimage hashes, central `workspace.write` permission and
  approval.
- Commands resolve only host-authored presets and whole-token parameters.
- A deterministic fake sandbox and fail-closed unsupported sandbox are included.
- A real process/container sandbox must be injected by the host. No unrestricted host
  subprocess fallback is implemented or claimed.

## Provider support

| Provider | Contract tests | Live in this validation | Notes |
|---|---:|---:|---|
| Fake | pass | n/a | Control flow only |
| OpenAI | pass | pass | `gpt-5.6-luna`, chat plus existing tool smoke |
| Gemini | pass | not run | No credential/model smoke recorded in this phase |
| Ollama | pass | not run | No local server/model confirmed in this phase |

Text-only, structured-output and local-only incompatibilities fail during preflight.
Unknown token/cost/time usage remains unknown rather than zero.

## Verification performed

- Python: 274 collected; 273 passed and one existing optional PostgreSQL integration
  skipped.
- Ruff format/check and strict mypy: pass.
- Node protocol/UI build, typecheck and tests: pass (2 protocol + 7 UI tests).
- Wheel and sdist build: pass; `twine check`: pass.
- Clean virtual environment install from wheel and `pip check`: pass.
- Installed-package prompt/template resource smoke: pass.
- Fake Coding Kit example from installed wheel environment: pass.
- Existing OpenAI tool smoke: pass, 233 tokens.
- Coding Kit OpenAI Turkish chat smoke: pass, 880 tokens; console displayed a Windows
  encoding artifact while the model response itself completed.

## Recovery, context and observability

Existing process recovery tests verify uncertain effects block rather than replay;
runtime durability and context compaction regressions pass. Coding manifests persist
version pins, prompt checksums, plan/project fingerprints and role refs as owner-scoped
artifacts. Coding activity maps to existing canonical events instead of creating a
second protocol or fabricated progress stream.

## Known constraints

- No bundled production process sandbox; the host must provide one.
- Gemini and Ollama were not live-smoked in this phase.
- Team composition and delegation are tested deterministically, not with a paid live
  multi-agent benchmark.
- Documentation helpers propose bounded create/update actions; they do not overwrite
  project files automatically.
- Publishing, tagging and deployment were not performed.

## Recommendation

The maintainer approved prerelease candidate `0.6.0a1` because the change adds
substantial opt-in public APIs. Publishing must still use the trusted release workflow.
