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
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.quality import CompletionGate, CompletionGateResult, CompletionGateStatus
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
    `required_tasks` are logical ids that must have completed and that every review must cover.
    """

    reviewer_workers: Mapping[str, str]
    required_tasks: frozenset[str] = frozenset()
    min_reviewers: int = 1
    require_evidence: bool = True
    require_fingerprint: bool = True
    human_approval: HumanApprovalPort | None = None
    max_scan_tasks: int = 200

    def __post_init__(self) -> None:
        if not self.reviewer_workers:
            raise ValueError("A team acceptance policy needs at least one reviewer worker")
        for worker_id, role in self.reviewer_workers.items():
            if not worker_id or not isinstance(role, str) or not role:
                raise ValueError("Each reviewer worker needs a role")
        if not 1 <= self.min_reviewers <= 10:
            raise ValueError("min_reviewers must be between 1 and 10")
        if not 1 <= self.max_scan_tasks <= _MAX_SCAN:
            raise ValueError(f"max_scan_tasks must be between 1 and {_MAX_SCAN}")
        object.__setattr__(self, "reviewer_workers", dict(self.reviewer_workers))


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
        if len(current) < policy.min_reviewers:
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
        for review in reviews:
            outcome = await self._check_review(review, run, by_id, latest, fingerprint, cycles)
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
        return _decide(
            TeamAcceptanceStatus.ACCEPTED, "accepted", ids=reviewer_ids, subject=subject_digest
        )

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
            again = await self.assembler.owned_references(
                review.evidence_refs, run, authenticate=True
            )
            if sorted(again) != sorted(review.evidence_refs):
                return _decide(TeamAcceptanceStatus.UNAVAILABLE, "evidence_unverifiable")
        return None


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


def _decide(
    status: TeamAcceptanceStatus,
    code: str,
    subject_task: str | None = None,
    *,
    ids: tuple[str, ...] = (),
    subject: str | None = None,
) -> TeamAcceptanceDecision:
    reasons = (f"{code}: {subject_task}",) if subject_task else (code,)
    return TeamAcceptanceDecision(
        status=status,
        code=code,
        reasons=reasons,
        reviewer_task_ids=ids,
        subject_digest=subject,
    )
