from __future__ import annotations

import asyncio
from collections.abc import Mapping
from time import monotonic
from typing import Any, Protocol

from kaiwen_agent.audit import SecretRedactor
from kaiwen_agent.evaluation.assertions import evaluate_assertion
from kaiwen_agent.evaluation.models import (
    EvaluationCase,
    EvaluationReport,
    EvaluationResult,
    EvaluationSuite,
)


class EvaluationTarget(Protocol):
    async def evaluate(self, case: EvaluationCase, *, simulation: bool) -> Mapping[str, Any]: ...


class EvaluationRunner:
    def __init__(
        self,
        *,
        target: EvaluationTarget,
        max_concurrency: int = 4,
        redactor: SecretRedactor | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.target = target
        self.redactor = redactor or SecretRedactor()
        self._limit = asyncio.Semaphore(max_concurrency)

    async def run(self, suite: EvaluationSuite, *, simulation: bool = True) -> EvaluationReport:
        case_ids = [case.id for case in suite.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Evaluation case ids must be unique within a suite")
        results = await asyncio.gather(
            *(self._run_case(case, simulation=simulation) for case in suite.cases)
        )
        return EvaluationReport(
            suite_id=suite.id,
            suite_version=suite.version,
            results=tuple(results),
        )

    async def _run_case(self, case: EvaluationCase, *, simulation: bool) -> EvaluationResult:
        started = monotonic()
        try:
            async with self._limit:
                raw_output = await self.target.evaluate(case, simulation=simulation)
            output = self.redactor.redact(dict(raw_output))
            if not isinstance(output, dict):  # pragma: no cover - dict input guarantees this
                raise TypeError("Redacted evaluation output must remain a mapping")
            assertions = tuple(
                evaluate_assertion(output, assertion) for assertion in case.assertions
            )
            return EvaluationResult(
                case_id=case.id,
                category=case.category,
                passed=all(result.passed for result in assertions),
                assertions=assertions,
                duration_ms=(monotonic() - started) * 1000,
            )
        except Exception as error:  # noqa: BLE001 - one case must not abort a suite
            return EvaluationResult(
                case_id=case.id,
                category=case.category,
                passed=False,
                assertions=(),
                error=str(self.redactor.redact(f"{type(error).__name__}: {error}")),
                duration_ms=(monotonic() - started) * 1000,
            )
