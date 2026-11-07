# Kaiwen Agent Platform — coding-agent guide

This repository is the reusable platform. Product-specific prompts, database
models, routes, branding, and tools belong in consuming applications.

## Package selection

- Python orchestration, providers, tools, persistence, tasks: `kaiwen-agent`.
- Shared event/action/artifact contracts: `@imthekaiwen/agent-protocol`.
- Browser state, transports, semantic client actions, React UI:
  `@imthekaiwen/agent-ui` and `@imthekaiwen/agent-ui/react`.

Read `docs/architecture.md`, `docs/roadmap.md`, and the accepted records under
`docs/decisions/` before changing boundaries. For a frontend integration, read
`packages/ui-agent/INTEGRATION.md`. For runnable patterns, use the matching directory
under `examples/`.

## Non-negotiable invariants

- API keys and provider SDK calls remain server-side.
- Tools and client actions are deny-by-default and explicitly registered.
- Side effects use authorization, approval, and idempotency where applicable.
- Browser actions are semantic; never transmit executable code or DOM selectors.
- Protocol changes update JSON Schema, TypeScript contracts, fixtures, and tests
  together.
- Reusable packages never import a product application.

## Verification

```powershell
npm.cmd run build
npm.cmd run typecheck
npm.cmd test
.\.venv\Scripts\python.exe -m pytest tests
.\.venv\Scripts\python.exe -m ruff check src tests examples scripts
.\.venv\Scripts\python.exe -m mypy src scripts/check_release.py
.\.venv\Scripts\python.exe scripts/check_release.py metadata
```

Prefer deterministic fakes and contract tests for repeatable automated testing, while
using real-provider integration tests when needed to verify actual external
compatibility.

Publishing is an explicit release action. Never publish from a feature task or
commit `.env`, tokens, generated databases, or local virtual environments.
