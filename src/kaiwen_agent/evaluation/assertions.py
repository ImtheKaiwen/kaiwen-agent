from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from kaiwen_agent.evaluation.models import (
    AssertionOperator,
    AssertionResult,
    EvaluationAssertion,
)

_MISSING = object()


def evaluate_assertion(
    output: Mapping[str, Any], assertion: EvaluationAssertion
) -> AssertionResult:
    actual = _resolve_path(output, assertion.path)
    operator = assertion.operator
    expected = assertion.expected
    try:
        if operator == AssertionOperator.EXISTS:
            passed = actual is not _MISSING
        elif operator == AssertionOperator.NOT_EXISTS:
            passed = actual is _MISSING
        elif operator == AssertionOperator.EQUALS:
            passed = actual is not _MISSING and actual == expected
        elif operator == AssertionOperator.NOT_EQUALS:
            passed = actual is not _MISSING and actual != expected
        elif operator == AssertionOperator.CONTAINS:
            passed = actual is not _MISSING and _contains(actual, expected)
        elif operator == AssertionOperator.NOT_CONTAINS:
            passed = actual is not _MISSING and not _contains(actual, expected)
        elif operator == AssertionOperator.LESS_THAN_OR_EQUAL:
            passed = actual is not _MISSING and actual <= expected
        else:  # pragma: no cover - enum validation makes this unreachable
            passed = False
    except (TypeError, ValueError):
        passed = False
    visible_actual = None if actual is _MISSING else actual
    return AssertionResult(
        assertion=assertion,
        passed=passed,
        actual=visible_actual,
        message="" if passed else f"Assertion failed at {assertion.path}",
    )


def _resolve_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not part or part.startswith("_"):
            return _MISSING
        if isinstance(current, Mapping):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            if not part.isdigit():
                return _MISSING
            index = int(part)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _contains(container: Any, expected: Any) -> bool:
    if isinstance(container, (str, bytes, Mapping, Sequence)):
        return expected in container
    return False
