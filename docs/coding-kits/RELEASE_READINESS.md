# Coding Kits release readiness

> Historical snapshot: this document records the state at the time it was written
> and is not updated as the code evolves. Current status is in `docs/roadmap.md`.

Status: **candidate, with documented environment gaps**.

## Passed gates

- Core API regressions, Coding Kits unit/integration tests, lint and strict typing.
- TypeScript protocol/UI build, typecheck and tests.
- Wheel/sdist construction and metadata validation.
- Clean wheel install, dependency check and installed-resource smoke.
- Secret-resource inspection: no `.env`, PEM, key or P12 in the wheel.
- Fake provider composition and small live OpenAI chat/tool smokes.

## Not verified in this phase

- Live Gemini workflow.
- Live Ollama workflow and locally installed model capability.
- Host-specific production sandbox/process cancellation.
- Paid live team benchmark and Lab UI rendering of every mapped event.

These gaps do not invalidate the fail-closed contracts, but they must not be described
as live-supported evidence. A release note must preserve those qualifications.

## Release action

No tag, PyPI/npm publish or deployment is authorized by this implementation task.
The approved candidate version is `0.6.0a1`. Rerun release CI, publish through the
existing trusted workflow, then cold-install the published artifact and repeat the
smoke.
