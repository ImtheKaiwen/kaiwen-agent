# Integration results

> Historical snapshot: this document records the state at the time it was written
> and is not updated as the code evolves. Current status is in `docs/roadmap.md`.

## Unified runtime

### A. Simple agent

The installed minimal runtime returned the expected response without registering
tools, teams, jobs, or evaluators. Unit coverage verifies system/context assembly,
history handling, and provider continuation isolation.

### B. Autonomous agent

Workflow integration executed dependent tasks, checkpoints, bounded retries,
replanning, quality repair, and terminal-state persistence. The Todo consumer ran
without interactive “continue” prompts and produced usable files plus passing tests.
The OpenAI live smoke validated a bounded real tool round trip. The deterministic
Todo worker flow still does not claim that an LLM independently authored the project.

### C. Agent team

Team tests verify immutable definitions, different agent permission sets,
least-privilege selection, capability-subset enforcement, bounded delegation,
structured handoff, concurrency limits, and cross-team pin rejection. The installed
team quickstart completed. Live heterogeneous-provider team execution was not run.

### D. Standards packs

Versioning, deterministic conditional resolution, conflict rejection, mandatory and
advisory obligations, fresh owner-scoped evidence, and completion blocking all pass.
The Todo consumer used a software standard and completion gate end to end. Standards
remain separate from security policy decisions.

### E. Context compaction

Tests verify provider-aware counting, schema-inclusive context limits, preservation of
recent raw messages and protected goals/decisions/constraints, rejection of secret or
unverifiable summaries, large-output offloading, and owner isolation.

### F. Checkpoint and recovery

SQLite tests cover tasks, workflow runs, approvals, action intents, receipts, and
uncertain side effects. `test_process_recovery.py` creates state in one Python process
and reads it in a second process. Completed idempotent effects are replayed from the
receipt; executing/uncertain effects are blocked rather than blindly repeated.

### G. Policy, permissions, and approval

Tests exercise deny-by-default policy, permission subset rules, approval precedence,
revalidated policy modifications, cross-user/cross-conversation approval rejection,
one-time approval consumption, and tenant isolation. The legal-domain validation
confirmed that denied approval prevents the handler from running.

### H. Evaluator and completion

Completion requires fresh, typed, owned evidence. Failed checks trigger finite repair
or replan decisions, repeated failures trigger stagnation, and exhausted limits block
completion. Scheduler and tool retry limits are independently bounded.

## Provider adapters

| Provider | Deterministic coverage | Live result |
| --- | --- | --- |
| OpenAI Responses | message/tool serialization, stateless and stored continuation, tool outputs, state isolation, structured schemas, failover rules | Pass; bounded real tool round trip, 233 tokens |
| Gemini | provider construction, native proposal conversion, central tool execution boundary | Not run; key missing |
| Ollama | native proposal conversion, explicit continuation, text-only capability denial | Not run; host/model missing |
| OpenAI Live | session configuration, normalized events, sideband delegation, approval boundary | No real media session run |

Unsupported or unavailable provider capabilities are not classified as runtime bugs.

## Package-consumer boundary

Fresh wheel and sdist were built into a temporary directory. Both passed `twine
check`; a new virtual environment installed the wheel and passed `pip check`.
Imports resolved from that environment's `site-packages`. The minimal, autonomous,
team, speech, Todo, and legal-domain examples all ran from the installed artifact.

The archives contain no `.env`, bytecode/cache files, private control-center plan, or
generated validation workspace. The source distribution intentionally includes the
test suite; the wheel contains runtime modules only.
