from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RepairDecision(StrEnum):
    REPAIR = "repair"
    STAGNATED = "stagnated"
    EXHAUSTED = "exhausted"


class RepairLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_repairs_per_requirement: int = Field(default=3, ge=0, le=100)
    max_replans: int = Field(default=2, ge=0, le=100)
    stagnation_repetitions: int = Field(default=2, ge=2, le=100)


class RepairTracker:
    """Deterministic bounds only; it does not execute repairs or replans."""

    def __init__(self, limits: RepairLimits | None = None) -> None:
        self.limits = limits or RepairLimits()
        self._attempts: dict[str, int] = {}
        self._fingerprints: dict[str, list[str]] = {}
        self._replans = 0

    def record_failure(self, requirement_id: str, fingerprint: str) -> RepairDecision:
        if not requirement_id or not fingerprint:
            raise ValueError("requirement_id and fingerprint must not be empty")
        attempts = self._attempts.get(requirement_id, 0)
        if attempts >= self.limits.max_repairs_per_requirement:
            return RepairDecision.EXHAUSTED
        history = self._fingerprints.setdefault(requirement_id, [])
        history.append(fingerprint)
        self._attempts[requirement_id] = attempts + 1
        repeated = 1
        for previous in reversed(history[:-1]):
            if previous != fingerprint:
                break
            repeated += 1
        if repeated >= self.limits.stagnation_repetitions:
            return RepairDecision.STAGNATED
        return RepairDecision.REPAIR

    def request_replan(self) -> bool:
        if self._replans >= self.limits.max_replans:
            return False
        self._replans += 1
        return True
