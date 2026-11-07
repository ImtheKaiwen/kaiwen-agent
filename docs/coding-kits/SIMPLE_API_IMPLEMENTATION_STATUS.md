# Simple Coding API implementation status

> Historical snapshot: this document records the state at the time it was written
> and is not updated as the code evolves. Current status is in `docs/roadmap.md`.

Baseline: published `kaiwen-agent==0.6.0a1`. The checkout and its accepted ADRs are
authoritative. The Simple API is additive and does not change existing constructors
or execution semantics.

| Capability | Existing implementation | Proposed adapter | Unit/integration test | Status | Evidence / open issue |
|---|---|---|---|---|---|
| Chat | `CodingKitFactory` → `CodingAgent` → `AgentRuntime.chat` | Lazy `CodingKit.agent().chat()` | `test_coding_simple_api.py` | Verified | Creates no workflow/team resources and exposes no tools. |
| Explicit action | `AgentRuntime.action` → `ToolExecutor` | Exact host allowlist and preflight | `test_coding_simple_api.py` | Verified | Workspace configuration alone grants nothing. |
| Provider composition | `ProviderRegistry`, optional provider adapters | Lazy local factory or injected `AgentFactory` | simple API and package tests | Verified | No universal model ID or implicit Ollama startup. |
| Model compatibility | `CodingProviderCatalog`, `ModelCapabilitiesRegistry` | Pure preflight projection | simple API security cases | Verified | Workflow fails without known structured/tool support. |
| Solo workflow | `ObjectivePlanner` → `WorkflowController` → `TaskScheduler` → `CompletionGate` | Objective/start adapter and durable handle | real SQLite workflow integration | Verified | No alternate execution loop. |
| Team workflow | `CodingTeamFactory`, `TeamRuntime`, `JobManager` | Explicit existing-team launcher port | preflight + existing team suites | Host-bound | No team creation on solo/chat path; no generic production launcher is fabricated. |
| Durable status | `DurableRunManager`, `RuntimeRunStore` | Owner-scoped `CodingRunHandle` | restart/owner/workspace tests | Verified | Durable run record remains source of truth. |
| Pause/cancel/resume | scheduler/job control plus durable run state | Host control/relaunch callbacks | recovery tests | Verified boundary | Unsupported operations fail explicitly. |
| Events | canonical `AgentEvent`, scoped event stores | Sanitized exact-run projection | existing event security suites | Reused | No hidden reasoning or cross-run event leakage. |
| Context/compaction | `ContextBuilder`, `ContextCompactionManager` | Existing runtime metadata/events | existing + simple API tests | Reused | No second summary system. |
| Standards/evidence | `StandardsResolver`, verifiers, `CompletionGate` | Resolve once and pass unchanged | workflow tests | Reused | Missing evidence remains blocking/inconclusive. |
| Security/recovery | policy, approval, idempotency, durable intents/receipts | Preflight plus existing execution path | security/recovery suites | Reused | No prompt-derived authority or blind external replay. |
| Backward compatibility | Published `0.6.0a1` APIs/examples | No changes to required parameters | compatibility tests | Guarded | Direct factory examples must remain runnable. |
| Clean install | Hatch wheel/sdist and packaged resources | Installed consumer examples | cold-install smoke | Verified | Built wheel installed in a clean venv; chat and workflow examples passed. |

## DX0 baseline evidence

- `python -m pytest backend/tests`: **273 passed, 1 optional PostgreSQL test skipped**.
- Package version before this extension: `0.6.0a1`.
- Root `kaiwen_agent` does not import Coding Kits.
- The untracked maintainer plan file is outside this implementation and remains
  untouched.

## DX8 evidence

- Full Python suite: **282 passed, 1 optional PostgreSQL test skipped**.
- Ruff format/check and strict mypy: passed.
- Node build, protocol check, tests, typecheck and npm dry-run packs: passed.
- Wheel/sdist build and Twine validation: passed.
- Clean virtual environment installed the built wheel and ran the chat and durable
  workflow examples successfully.
- One minimal OpenAI facade chat returned text and usage. Gemini and an installed
  Ollama server remain explicitly unverified.
