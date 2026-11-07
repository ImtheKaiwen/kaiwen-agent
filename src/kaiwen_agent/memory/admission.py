from __future__ import annotations

import re

from kaiwen_agent.memory.models import (
    MemoryAdmissionDecision,
    MemoryAdmissionResult,
    MemoryCandidate,
    MemorySensitivity,
)


class SecretDetector:
    """Conservative local filter that runs before policy, storage, or embeddings."""

    _patterns = (
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*", re.IGNORECASE),
        re.compile(
            r"\b(?:api[_ -]?key|client[_ -]?secret|password|access[_ -]?token)\s*"
            r"[:=]\s*[^\s,;]{8,}",
            re.IGNORECASE,
        ),
    )
    _secret_key = re.compile(
        r"(^|_)(api_?key|authorization|cookie|password|private_?key|secret|token)($|_)",
        re.IGNORECASE,
    )

    def contains_secret(self, candidate: MemoryCandidate) -> bool:
        if candidate.sensitivity == MemorySensitivity.SECRET:
            return True
        if self._contains_secret_key(candidate.structured_data):
            return True
        serialized = f"{candidate.content}\n{candidate.structured_data}"
        return self.contains_secret_text(serialized)

    def contains_secret_text(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in self._patterns)

    def _contains_secret_key(self, value: object) -> bool:
        if isinstance(value, dict):
            return any(
                self._secret_key.search(str(key)) is not None or self._contains_secret_key(item)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(self._contains_secret_key(item) for item in value)
        return False


class MemoryAdmissionEngine:
    def __init__(
        self,
        *,
        auto_save_threshold: float = 0.72,
        review_threshold: float = 0.48,
        secret_detector: SecretDetector | None = None,
    ) -> None:
        if not 0 <= review_threshold <= auto_save_threshold <= 1:
            raise ValueError("thresholds must satisfy 0 <= review <= auto_save <= 1")
        self.auto_save_threshold = auto_save_threshold
        self.review_threshold = review_threshold
        self.secret_detector = secret_detector or SecretDetector()

    def evaluate(self, candidate: MemoryCandidate) -> MemoryAdmissionResult:
        if self.secret_detector.contains_secret(candidate):
            return MemoryAdmissionResult(
                decision=MemoryAdmissionDecision.DISCARD,
                score=0,
                reasons=["secret_or_credential_detected"],
            )
        score = (
            candidate.future_utility * 0.25
            + candidate.stability * 0.2
            + candidate.confidence * 0.2
            + candidate.explicitness * 0.2
            + candidate.scope_relevance * 0.15
        )
        if candidate.sensitivity == MemorySensitivity.SENSITIVE:
            return MemoryAdmissionResult(
                decision=MemoryAdmissionDecision.REVIEW,
                score=score,
                reasons=["sensitive_content_requires_review"],
            )
        if score >= self.auto_save_threshold:
            decision = MemoryAdmissionDecision.AUTO_SAVE
            reason = "admission_score_allows_save"
        elif score >= self.review_threshold:
            decision = MemoryAdmissionDecision.REVIEW
            reason = "admission_score_requires_review"
        else:
            decision = MemoryAdmissionDecision.DISCARD
            reason = "admission_score_below_threshold"
        return MemoryAdmissionResult(decision=decision, score=score, reasons=[reason])
