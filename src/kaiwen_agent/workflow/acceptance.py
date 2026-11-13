"""Host-controlled team acceptance (ADR 0017, M6.3.4).

`TeamAcceptance.evaluate` is called by `WorkflowController` only after the completion gate has
passed, and it re-reads everything it uses from authoritative storage. It decides whether a whole
team's delivery satisfies a host-configured contract:

- the required tasks completed (and none of them failed or was cancelled);
- the current reviewer tasks are tasks of the host's reviewer workers, proven (compiler-derived id,
  the run's owner and session, COMPLETED) and each holds a verified review record;
- each review is bound to the exact outputs it reviewed and to the current protected fingerprint;
- every reviewer recommends approval, cites authenticated evidence (when required) and covers the
  required tasks;
- a human approval, when required, is granted by the host's own source for exactly this delivery.

It never replaces the completion gate (technical requirements stay the gate's), never grants
authority from a model-authored word, and writes nothing. The decision is recomputed at every
final boundary; nothing here is a durable approval or a new authority store. A restart re-reads
the same stored task outputs and recomputes the same decision, or a different one if anything
changed.

Precedence of the answer: `unavailable` (the host cannot establish a fact), then `blocked`, then
`changes_required`, then `approval_required`, then `accepted`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.quality import (
    CompletionGate,
    CompletionGateResult,
    CompletionGateStatus,
    VerificationStatus,
)
from kaiwen_agent.runtime import DurableRunManager, RuntimeRunRecord
from kaiwen_agent.tasks import AgentTask, TaskRunQuery, TaskStatus
from kaiwen_agent.workflow.models import TeamAcceptanceDecision, TeamAcceptanceStatus
from kaiwen_agent.workflow.review import (
    ReviewRecordError,
    VerifiedReview,
    digest_of,
    output_digest,
    verify_review_record,
)
from kaiwen_agent.workflow.run_tasks import RunTaskScanError, scan_run_tasks
from kaiwen_agent.workflow.task_context import (
    TaskContextAssembler,
    _same_authority,
    proven_cycle,
)

logger = logging.getLogger(__name__)

_MAX_SCAN = 500


class EvidenceMode(StrEnum):
    """What cited reviewer evidence must be, when `require_evidence` is set.

    `GATE_VERIFIED` (the default): every cited reference must be among the references the
    authoritative completion gate verified (`PASS` verifications only). `AUTHENTIC_RUN_LEGACY` is
    the weaker pre-hardening contract, an explicit host opt-in: any signed, trusted-origin artifact
    of this run counts, whether or not the gate relied on it. It must never be the default.
    """

    GATE_VERIFIED = "gate_verified"
    AUTHENTIC_RUN_LEGACY = "authentic_run_legacy"


class HumanApprovalStatus(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    PENDING = "pending"


class HumanApproval(BaseModel):
    """The host's answer for one exact delivery. `subject_digest` must echo the asked subject."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: HumanApprovalStatus
    subject_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ApprovalSubject:
    """What a human approval is for: this owner, this run, this workspace state, these outputs.

    `digest` covers every other field, so an approval granted for other content, another run,
    another workspace or another fingerprint does not match. The platform stores no approval; the
    host's own authorization system decides and echoes `digest` back.
    """

    tenant_id: str
    user_id: str
    session_id: str
    run_id: str
    workspace_id: str | None
    fingerprint: str | None
    reviewed: tuple[tuple[str, str], ...]  # (task id, output digest), sorted
    review_digests: tuple[str, ...]  # sorted
    digest: str


class HumanApprovalPort(Protocol):
    """Host-supplied source of a human decision for one delivery. Read-only for the platform."""

    async def decision(self, subject: ApprovalSubject) -> HumanApproval: ...


@dataclass(frozen=True, slots=True)
class TeamAcceptancePolicy:
    """Host configuration of the acceptance contract. Nothing here comes from a plan or a model.

    `reviewer_workers` maps the worker ids authorized as reviewers to the role the host expects of
    them (checked against the registered worker's own `role` when it declares one).
    `required_tasks` are logical ids that must have completed and that every review must cover. An
    empty set would silently imply a reviewed delivery, so it is refused unless the host sets
    `allow_partial_review=True`: a review then certifies only the tasks it names, never every task.
    `evidence_mode` defaults to evidence the gate itself verified (see `EvidenceMode`).
    """

    reviewer_workers: Mapping[str, str]
    required_tasks: frozenset[str] = frozenset()
    allow_partial_review: bool = False
    min_reviewers: int = 1
    require_evidence: bool = True
    evidence_mode: EvidenceMode = EvidenceMode.GATE_VERIFIED
    require_fingerprint: bool = True
    human_approval: HumanApprovalPort | None = None
    max_scan_tasks: int = 200

    def __post_init__(self) -> None:
        if not self.reviewer_workers:
            raise ValueError("A team acceptance policy needs at least one reviewer worker")
        for worker_id, role in self.reviewer_workers.items():
            if not worker_id or not isinstance(role, str) or not role:
                raise ValueError("Each reviewer worker needs a role")
        if not self.required_tasks and not self.allow_partial_review:
            raise ValueError(
                "required_tasks must name the tasks every review covers; "
                "set allow_partial_review=True only for an intentionally partial review"
            )
        if not 1 <= self.min_reviewers <= 10:
            raise ValueError("min_reviewers must be between 1 and 10")
        if not 1 <= self.max_scan_tasks <= _MAX_SCAN:
            raise ValueError(f"max_scan_tasks must be between 1 and {_MAX_SCAN}")
        # A read-only copy: the validated mapping cannot change after construction.
        object.__setattr__(self, "reviewer_workers", MappingProxyType(dict(self.reviewer_workers)))
        object.__setattr__(self, "required_tasks", frozenset(self.required_tasks))


@dataclass(frozen=True, slots=True)
class TeamAcceptance:
    """Opt-in controller configuration. Disabled when absent.

    `tasks` and `runs` must be the controller's own authoritative fenced storage (checked by the
    controller when it is built). `assembler` supplies the existing owner/run/signature rules used
    to re-verify cited evidence.
    """

    policy: TeamAcceptancePolicy
    tasks: TaskRunQuery
    runs: DurableRunManager
    assembler: TaskContextAssembler

    def composition_issues(self, completion_gate: object) -> list[str]:
        """Why this acceptance does not verify evidence like the workflow's completion gate.

        Identity only; matching ids, paths, protocols or flags never count. The task store must
        be the authoritative one. When evidence is required, the assembler must authenticate with
        the very artifact manager the platform `CompletionGate` reads and the very signer its
        evidence verifiers use (the same relation `CodingWorkspace` already requires of a
        controller). A gate that is not the platform `CompletionGate` cannot be inspected, so it
        cannot be proven consistent and is refused rather than trusted.
        """

        issues: list[str] = []
        if not _same_authority(self.assembler.store, self.tasks):
            issues.append("assembler_reads_a_different_task_store")
        if not self.policy.require_evidence:
            return issues
        signer = self.assembler.evidence_signer
        if signer is None:
            issues.append("assembler_has_no_evidence_signer")
        if type(completion_gate) is not CompletionGate:
            issues.append("completion_gate_evidence_pipeline_not_inspectable")
            return issues
        if self.assembler.artifacts is not completion_gate.artifacts:
            issues.append("assembler_uses_different_artifacts")
        verifier_signers = [
            getattr(completion_gate.verifiers.get(verifier_id), "signer", None)
            for verifier_id in ("coding.tests", "coding.acceptance")
        ]
        trusted = [candidate for candidate in verifier_signers if candidate is not None]
        if not trusted:
            issues.append("completion_gate_has_no_signed_evidence_verifier")
        elif signer is not None and any(candidate is not signer for candidate in trusted):
            issues.append("assembler_uses_a_different_evidence_signer")
        return issues

    async def evaluate(
        self,
        *,
        run: RuntimeRunRecord,
        gate: CompletionGateResult,
        fingerprint: str | None,
    ) -> TeamAcceptanceDecision:
        failure: str | None = None
        try:
            return await self._evaluate(run=run, gate=gate, fingerprint=fingerprint)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - fail closed; the message is never used
            failure = type(error).__name__
        logger.warning("team acceptance failure error_type=%s run=%s", failure, run.id)
        return _decide(TeamAcceptanceStatus.UNAVAILABLE, "evaluation_failed")

    async def _evaluate(
        self, *, run: RuntimeRunRecord, gate: CompletionGateResult, fingerprint: str | None
    ) -> TeamAcceptanceDecision:
        policy = self.policy
        if gate.status is not CompletionGateStatus.PASS:
            # The gate keeps sole authority over technical requirements.
            return _decide(TeamAcceptanceStatus.BLOCKED, "gate_not_passed")
        scan_failure: str | None = None
        try:
            scan = await scan_run_tasks(self.tasks, run, max_tasks=policy.max_scan_tasks)
        except RunTaskScanError as error:
            scan_failure = (
                "inventory_invalid"
                if error.code == "task_listing_invalid"
                else "inventory_unavailable"
            )
        if scan_failure is not None:
            return _decide(TeamAcceptanceStatus.UNAVAILABLE, scan_failure)
        if scan.truncated or scan.unreadable > 0:
            return _decide(TeamAcceptanceStatus.UNAVAILABLE, "inventory_incomplete")
        cycles: dict[str, int] = {}
        for row in scan.tasks:
            cycle = proven_cycle(row, run)
            if cycle is None:
                return _decide(TeamAcceptanceStatus.UNAVAILABLE, "task_identity_invalid")
            cycles[row.id] = cycle
        by_id = {row.id: row for row in scan.tasks}

        # Required tasks: the latest task of each logical id must have completed.
        latest: dict[str, AgentTask] = {}
        for row in scan.tasks:
            logical = str(row.metadata["logical_task_id"])
            known = latest.get(logical)
            if known is None or cycles[row.id] > cycles[known.id]:
                latest[logical] = row
        for logical in sorted(policy.required_tasks):
            needed = latest.get(logical)
            if needed is None:
                return _decide(TeamAcceptanceStatus.BLOCKED, "required_task_missing", logical)
            if needed.status is not TaskStatus.COMPLETED:
                return _decide(TeamAcceptanceStatus.BLOCKED, "required_task_not_completed", logical)

        # Reviewers: tasks of the host's reviewer workers, in the latest cycle that has one.
        candidates = [r for r in scan.tasks if r.agent_id in policy.reviewer_workers]
        if not candidates:
            return _decide(TeamAcceptanceStatus.BLOCKED, "review_missing")
        newest = max(cycles[r.id] for r in candidates)
        current = sorted((r for r in candidates if cycles[r.id] == newest), key=lambda r: r.id)
        if any(r.status is not TaskStatus.COMPLETED for r in current):
            return _decide(TeamAcceptanceStatus.BLOCKED, "review_not_completed")
        if len({r.agent_id for r in current}) < policy.min_reviewers:
            return _decide(TeamAcceptanceStatus.BLOCKED, "review_missing")
        workspace_id = run.workspace_binding.workspace_id if run.workspace_binding else None
        reviews: list[VerifiedReview] = []
        for reviewer in current:
            try:
                reviews.append(verify_review_record(reviewer, run, workspace_id=workspace_id))
            except ReviewRecordError as error:
                return _decide(TeamAcceptanceStatus.UNAVAILABLE, error.code)
        reviewer_ids = tuple(r.id for r in current)
        if policy.require_fingerprint and not fingerprint:
            return _decide(
                TeamAcceptanceStatus.UNAVAILABLE, "fingerprint_unavailable", ids=reviewer_ids
            )

        problems: list[TeamAcceptanceDecision] = []
        verified = _gate_verified_refs(gate)
        for review in reviews:
            outcome = await self._check_review(
                review, run, by_id, latest, fingerprint, cycles, verified
            )
            if outcome is None:
                continue
            if outcome.status is TeamAcceptanceStatus.UNAVAILABLE:
                return outcome.model_copy(update={"reviewer_task_ids": reviewer_ids})
            problems.append(outcome)
        for wanted in (TeamAcceptanceStatus.BLOCKED, TeamAcceptanceStatus.CHANGES_REQUIRED):
            # Precedence: blocked over changes_required; within a status, the first reviewer's.
            found = next((p for p in problems if p.status is wanted), None)
            if found is not None:
                return found.model_copy(update={"reviewer_task_ids": reviewer_ids})
        # Here every current review recommends approval: any other recommendation, and so any
        # disagreement between reviewers, was already returned above.
        approval = policy.human_approval
        subject_digest: str | None = None
        if approval is not None:
            subject = _subject(run, workspace_id, fingerprint, reviews)
            subject_digest = subject.digest
            answer = await self._ask(approval, subject)
            if answer is None:
                return _decide(
                    TeamAcceptanceStatus.UNAVAILABLE,
                    "approval_unavailable",
                    ids=reviewer_ids,
                    subject=subject_digest,
                )
            if answer.status is HumanApprovalStatus.PENDING:
                return _decide(
                    TeamAcceptanceStatus.APPROVAL_REQUIRED,
                    "human_approval_pending",
                    ids=reviewer_ids,
                    subject=subject_digest,
                )
            if answer.status is HumanApprovalStatus.DENIED:
                return _decide(
                    TeamAcceptanceStatus.BLOCKED,
                    "human_denied",
                    ids=reviewer_ids,
                    subject=subject_digest,
                )
            if answer.subject_digest != subject.digest:
                # An approval for other content, owner, run, workspace or fingerprint.
                return _decide(
                    TeamAcceptanceStatus.BLOCKED,
                    "approval_mismatch",
                    ids=reviewer_ids,
                    subject=subject_digest,
                )
            if policy.require_evidence:
                # The approval may have been awaited for a long time: evidence authenticated
                # before it can have been deleted or expired since, with the fingerprint intact.
                for review in reviews:
                    if not await self._evidence_authentic(review, run):
                        return _decide(
                            TeamAcceptanceStatus.UNAVAILABLE,
                            "evidence_unverifiable",
                            ids=reviewer_ids,
                            subject=subject_digest,
                        )
        cited = sorted({ref for review in reviews for ref in review.evidence_refs})
        return _decide(
            TeamAcceptanceStatus.ACCEPTED,
            "accepted",
            ids=reviewer_ids,
            subject=subject_digest,
            evidence=tuple(cited),
        )

    def gate_evidence_issue(
        self, decision: TeamAcceptanceDecision, gate: CompletionGateResult
    ) -> str | None:
        """Whether a later gate result still verifies the evidence an acceptance relied on.

        Used by the controller after a slow human approval, when the gate is asked again: the
        relationship "cited evidence is among the gate's verified evidence" is judged against the
        new result, never assumed from the first. Returns a stable code, or None when it holds.
        """

        policy = self.policy
        if not policy.require_evidence or policy.evidence_mode is not EvidenceMode.GATE_VERIFIED:
            return None
        verified = _gate_verified_refs(gate)
        if not verified:
            return "no_gate_verified_evidence"
        if not decision.evidence_refs or not set(decision.evidence_refs) <= verified:
            return "evidence_not_gate_verified"
        return None

    async def _ask(self, port: HumanApprovalPort, subject: ApprovalSubject) -> HumanApproval | None:
        failure: str | None = None
        try:
            answer = await port.decision(subject)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the message may carry secrets
            failure = type(error).__name__
            answer = None
        if failure is not None:
            logger.warning("human approval source failure error_type=%s", failure)
            return None
        return answer if isinstance(answer, HumanApproval) else None

    async def _check_review(
        self,
        review: VerifiedReview,
        run: RuntimeRunRecord,
        by_id: Mapping[str, AgentTask],
        latest: Mapping[str, AgentTask],
        fingerprint: str | None,
        cycles: Mapping[str, int],
        verified: frozenset[str],
    ) -> TeamAcceptanceDecision | None:
        policy = self.policy
        if review.assessment.recommendation == "blocked":
            return _decide(TeamAcceptanceStatus.BLOCKED, "reviewer_blocked")
        covered = {str(item["logical_id"]) for item in review.reviewed}
        for item in review.reviewed:
            source = by_id.get(str(item["task_id"]))
            if source is None or source.status is not TaskStatus.COMPLETED:
                return _decide(TeamAcceptanceStatus.CHANGES_REQUIRED, "reviewed_work_changed")
            if source.agent_id in policy.reviewer_workers:
                return _decide(TeamAcceptanceStatus.BLOCKED, "self_review")
            if output_digest(source) != item["output_digest"]:
                return _decide(TeamAcceptanceStatus.CHANGES_REQUIRED, "reviewed_work_changed")
            if latest.get(str(item["logical_id"])) is not source or (
                cycles[source.id] != item["cycle"]
            ):
                return _decide(TeamAcceptanceStatus.CHANGES_REQUIRED, "reviewed_work_superseded")
        if not policy.required_tasks <= covered:
            return _decide(TeamAcceptanceStatus.CHANGES_REQUIRED, "review_coverage_incomplete")
        if policy.require_fingerprint and review.fingerprint != fingerprint:
            return _decide(TeamAcceptanceStatus.CHANGES_REQUIRED, "review_stale")
        if review.assessment.recommendation == "changes_requested":
            return _decide(TeamAcceptanceStatus.CHANGES_REQUIRED, "reviewer_requested_changes")
        if policy.require_evidence:
            if not review.evidence_refs:
                return _decide(TeamAcceptanceStatus.CHANGES_REQUIRED, "no_authenticated_evidence")
            if policy.evidence_mode is EvidenceMode.GATE_VERIFIED:
                if not verified:
                    return _decide(TeamAcceptanceStatus.BLOCKED, "no_gate_verified_evidence")
                if review.unverified_references or not set(review.evidence_refs) <= verified:
                    return _decide(
                        TeamAcceptanceStatus.CHANGES_REQUIRED, "evidence_not_gate_verified"
                    )
            if not await self._evidence_authentic(review, run):
                return _decide(TeamAcceptanceStatus.UNAVAILABLE, "evidence_unverifiable")
        return None

    async def _evidence_authentic(self, review: VerifiedReview, run: RuntimeRunRecord) -> bool:
        again = await self.assembler.owned_references(review.evidence_refs, run, authenticate=True)
        return sorted(again) == sorted(review.evidence_refs)


def _subject(
    run: RuntimeRunRecord,
    workspace_id: str | None,
    fingerprint: str | None,
    reviews: Iterable[VerifiedReview],
) -> ApprovalSubject:
    reviewed = sorted(
        {(str(item["task_id"]), str(item["output_digest"])) for r in reviews for item in r.reviewed}
    )
    digests = sorted(r.digest for r in reviews)
    body = {
        "tenant_id": run.tenant_id,
        "user_id": run.user_id,
        "session_id": run.session_id,
        "run_id": run.id,
        "workspace_id": workspace_id,
        "fingerprint": fingerprint,
        "reviewed": reviewed,
        "review_digests": digests,
    }
    return ApprovalSubject(
        tenant_id=run.tenant_id,
        user_id=run.user_id,
        session_id=run.session_id,
        run_id=run.id,
        workspace_id=workspace_id,
        fingerprint=fingerprint,
        reviewed=tuple(reviewed),
        review_digests=tuple(digests),
        digest=digest_of(["kaiwen.team-acceptance.v1", body]),
    )


def _gate_verified_refs(gate: CompletionGateResult) -> frozenset[str]:
    """References of the gate's own `PASS` verifications; failed, inconclusive and
    approval-required verifications contribute nothing, and nothing model-authored is read."""

    return frozenset(
        ref
        for verification in gate.verifications
        if verification.status is VerificationStatus.PASS
        for ref in verification.evidence_refs
    )


def _decide(
    status: TeamAcceptanceStatus,
    code: str,
    subject_task: str | None = None,
    *,
    ids: tuple[str, ...] = (),
    subject: str | None = None,
    evidence: tuple[str, ...] = (),
) -> TeamAcceptanceDecision:
    reasons = (f"{code}: {subject_task}",) if subject_task else (code,)
    return TeamAcceptanceDecision(
        status=status,
        code=code,
        reasons=reasons,
        reviewer_task_ids=ids,
        subject_digest=subject,
        evidence_refs=evidence,
    )
