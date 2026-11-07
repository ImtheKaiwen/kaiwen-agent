# Validation report

> Historical snapshot: this document records the state at the time it was written
> and is not updated as the code evolves. Current status is in `docs/roadmap.md`.

Validated on 2026-10-17 from local commit `f1c117b` plus the validation changes in
the working tree. This report distinguishes deterministic integration coverage from
live-provider verification.

## Environment and secrets

- Python 3.12.13, Node.js 22.16.0, npm 10.9.2.
- A root `.env` exists and is ignored by Git. Package archives contain no `.env`,
  bytecode, validation output, or private planning files.
- `OPENAI_API_KEY` is defined. The low-token live smoke completed with
  `gpt-5.6-luna`, exercised `connectivity.check`, returned the expected
  `KAIWEN_AGENT_OK` marker, and used 233 tokens. No key material is recorded here.
- Neither `GEMINI_API_KEY` nor `GOOGLE_API_KEY` is defined.
- `OLLAMA_HOST` is not defined, and no Ollama live service/model was available.

## Executed checks

| Check | Result |
| --- | --- |
| `ruff format --check backend examples` | Pass; 265 files formatted |
| `ruff check backend examples` | Pass |
| `mypy backend/src` | Pass; 209 source files |
| `pytest backend/tests -q` | Pass; 230 passed, 1 skipped |
| Critical runtime/security subset | Pass; 53 tests |
| Provider/runtime contract subsets | Pass; 28 + 24 tests |
| `npm run build` | Pass |
| `npm run check:protocol` | Pass; 3 canonical schemas |
| `npm test` | Pass; 7 tests |
| `npm run typecheck` | Pass |
| `npm run pack:check` | Pass |
| GitHub CI on Linux | Pass; run `38996905653`, Python and Node jobs |
| Fresh wheel and sdist build | Pass |
| `twine check` | Pass for wheel and sdist |
| Fresh virtual environment install + `pip check` | Pass |
| Python dependency audit after updating the temporary environment's pip | No known vulnerabilities |
| `npm audit --audit-level=low` | One low-severity development-only `esbuild` advisory |

The PostgreSQL integration test is the single skipped test because no PostgreSQL
test DSN was provided. SQLite durability is covered, including a new test that writes
in one operating-system process and recovers task/checkpoint state in another.

## Installed-package consumer checks

A fresh virtual environment installed the newly built wheel, not the source tree.
The following examples then ran successfully:

- minimal chat runtime;
- autonomous workflow;
- least-privilege team delegation;
- speech pipeline;
- isolated Todo CLI workflow;
- isolated legal-domain policy scenario.

The Todo validation created a modular package, CRUD tests, README, plan, and GitHub
Actions workflow inside a temporary allowlisted workspace. Its four tasks
(`plan`, `code`, `test`, `review`) completed and the completion gate accepted only the
fresh owned test artifact. This is a deterministic host-worker integration test; it
does not prove live LLM code generation.

The legal scenario used fictional documents. It produced a source-referenced summary,
denied path traversal into a non-allowlisted directory, and proved that a denied
human approval resulted in zero external-action executions.

## Defects and gaps found

1. Fourteen existing Python files did not satisfy the configured formatter. They were
   formatted without behavioral changes, and CI/release now check formatting.
2. Recovery was previously tested by reopening SQLite within one process. A true
   subprocess boundary regression was added and passes.
3. Release CI did not execute the isolated consumer/domain validations. Both are now
   included in CI and the release verification job.
4. `npm audit` reports `GHSA-g7r4-m6w7-qqqr` through `tsup -> esbuild 0.27.7`.
   The affected component is a development build server, not shipped runtime code.
   The fixed `tsup` major requires a newer Node range than this repository currently
   promises, so no incompatible forced upgrade was made.
5. Unpinned formatter/type-checker versions produced platform-dependent CI results.
   Ruff and mypy are now pinned, mypy runs without incremental cache in CI, and the
   resulting Linux-only typing findings were fixed.
6. A polling-watch test compared a caller timestamp with a later creation timestamp.
   The test now polls from the watch's actual due time and passed 25 consecutive local
   repetitions plus the Linux CI suite.

## Live-provider status

- OpenAI: **pass**. A real low-token Responses request completed with a tool call and
  the expected final response. Contract, tool continuation, structured tool proposal,
  sideband, streaming-event, timeout/failover, and state-isolation behavior are also
  covered by deterministic tests.
- Gemini: **not run** because no credential is configured. Adapter contract tests pass.
- Ollama: **not run** because no host/model is configured. Native-tool and text-only
  capability tests pass.

No public release was performed.
