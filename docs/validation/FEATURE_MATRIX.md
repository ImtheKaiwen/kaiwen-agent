# Feature validation matrix

> Historical snapshot: this document records the state at the time it was written
> and is not updated as the code evolves. Current status is in `docs/roadmap.md`.

| Capability | Evidence | Status | Release impact |
| --- | --- | --- | --- |
| Minimal single-agent chat and system context | `test_unified_runtime.py`, installed `simple-runtime` | Pass | None |
| Conversation/provider state isolation | `test_agent.py`, concurrent provider-state regression | Pass | None |
| Autonomous multi-step workflow | `test_workflow_runtime.py`, installed autonomous and Todo examples | Pass | None |
| Tool validation, permissions, approval, retry, audit | `test_tool_pipeline.py`, `test_policy.py` | Pass | None |
| Bounded replan, repair, stagnation, completion | `test_workflow_runtime.py`, `test_standards_quality.py` | Pass | None |
| Team definition, least-privilege delegation, handoff | `test_teams.py`, `test_delegation.py`, installed team example | Pass | None |
| Parallel scheduling and concurrency limits | `test_tasks.py`, `test_delegation.py`, multitenant matrix | Pass | None |
| Standards packs and evidence gates | `test_standards_quality.py`, Todo completion gate | Pass | None |
| Context budgets and compaction | `test_context_memory.py`, `test_runtime_context_compaction.py` | Pass | None |
| Durable checkpoint/recovery | `test_runtime_durability.py`, `test_process_recovery.py` | Pass | None |
| Durable one-time approval/idempotency | `test_runtime_durability.py` | Pass | None |
| Tenant/user isolation | persistence, jobs, realtime, memory, multitenant matrix tests | Pass | None |
| Mid-flight mailbox instruction | `test_tasks.py::test_mailbox_pause_resume_instruction_and_checkpoint` | Pass | None |
| Evaluator/quality gate | `test_standards_quality.py`, `test_workflow_runtime.py` | Pass | None |
| Token/cost budget accounting | budget and production-control tests | Pass | None |
| Provider failover and locality | `test_model_failover.py` | Pass | None |
| OpenAI Responses adapter | contract tests | Pass | Live auth unverified |
| OpenAI live request | `examples/openai-smoke/main.py`; 233-token tool round trip | Pass | None |
| Gemini adapter | contract tests | Pass | Live behavior unverified |
| Ollama adapter | contract tests | Pass | Live behavior unverified |
| Realtime/OpenAI Live sideband | `test_openai_live_provider.py`, realtime suites | Pass | Real media session unverified |
| Speech abstraction | speech tests and installed example | Pass | Real STT/TTS engine unverified |
| Telephony contracts | telephony tests | Pass | Provider provisioning intentionally external |
| PostgreSQL memory adapter | optional integration test | Not run | Needs test DSN before production claim |
| Redis distributed leases | contract/unit coverage | Not live-tested | Needs Redis/load validation for multi-node claim |
| UI protocol/runtime packages | build, schemas, 7 tests, typecheck, pack dry-run | Pass | Low dev dependency advisory |
| Isolated Todo consumer | fresh wheel + temporary workspace | Pass | Deterministic, not live LLM-generated |
| Legal-domain configurability | fresh wheel + fictional documents | Pass | Not a legal-accuracy evaluation |

“Pass” means the listed behavior was executed. It does not imply that an optional
external provider was contacted unless the evidence explicitly says live.
