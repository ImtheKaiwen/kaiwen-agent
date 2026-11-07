from __future__ import annotations

from typing import Protocol

from kaiwen_agent.quality.models import VerificationContext, VerificationResult
from kaiwen_agent.standards import ResolvedRequirement


class Verifier(Protocol):
    verifier_id: str

    async def verify(
        self,
        requirement: ResolvedRequirement,
        *,
        context: VerificationContext,
    ) -> VerificationResult: ...


class VerifierRegistry:
    def __init__(self) -> None:
        self._verifiers: dict[str, Verifier] = {}

    def register(self, verifier: Verifier) -> None:
        if not verifier.verifier_id:
            raise ValueError("verifier_id must not be empty")
        if verifier.verifier_id in self._verifiers:
            raise ValueError(f"Verifier already registered: {verifier.verifier_id}")
        self._verifiers[verifier.verifier_id] = verifier

    def get(self, verifier_id: str) -> Verifier | None:
        return self._verifiers.get(verifier_id)
