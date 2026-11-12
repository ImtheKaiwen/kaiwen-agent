"""Typed reviewer assessments, recorded in the reviewer task's own output (ADR 0017, M6.3.4).

A reviewer is an ordinary `AgentRuntimeWorker` given one more registered tool, `review.submit`. The
tool stages one bounded `ReviewAssessment` in an execution-local `ReviewSession`; a successful
execution finalizes it into `output["review_assessment"]`, which the existing scheduler persists
with COMPLETED in one task write (through the fenced task store in fenced mode). Nothing new is
stored.

An assessment is a recommendation and a set of untrusted findings. It carries no authority: the
host binds it to the run, owner, workspace, the reviewer's own task id, the exact outputs of the
tasks it reviewed (a digest of each stored output) and the host-derived fingerprint, and
`TeamAcceptance` later re-reads all of it from storage. Model-authored fields cannot name a run,
owner, task id, workspace, permission, evidence flag or approval; unknown fields are rejected.

Honest limits: the digest detects change and truncation; it is not a signature, and a writer already
trusted to write tasks can forge a consistent record (the existing trusted-write boundary). A
reviewer that cites evidence only keeps references the host authenticated with the existing
`EvidenceSigner` rules; authentic is not accepted, and only the completion gate judges requirements.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kaiwen_agent.context import AgentContext
from kaiwen_agent.runtime import RuntimeRunRecord
from kaiwen_agent.tasks import AgentTask, TaskStatus
from kaiwen_agent.tools.definition import ToolDefinition

logger = logging.getLogger(__name__)

REVIEW_TOOL = "review.submit"
REVIEW_PERMISSION = "review.submit"
REVIEW_SESSION_SERVICE = "kaiwen.review_session"
REVIEW_OUTPUT_KEY = "review_assessment"
REVIEW_RECORD_VERSION = 1
MAX_REVIEWED = 10
_ID = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"
_REF = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$"
_HEX64 = r"^[0-9a-f]{64}$"
_MAX_RECORD_BYTES = 64_000

Recommendation = Literal["approve", "changes_requested", "blocked"]
LogicalId = Annotated[str, Field(min_length=1, max_length=200, pattern=_ID)]
Text = Annotated[str, Field(min_length=1, max_length=500)]
EvidenceRef = Annotated[str, Field(min_length=1, max_length=200, pattern=_REF)]


class ReviewRecordError(PermissionError):
    """A review record or a review submission cannot be trusted. `code` is stable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CriterionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion: Text
    verdict: Literal["met", "not_met", "unknown"]
    note: str = Field(default="", max_length=500)


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: Literal["info", "minor", "major", "blocking"]
    text: Text
    task: LogicalId | None = None
    evidence_refs: tuple[EvidenceRef, ...] = Field(default=(), max_length=5)


class ReviewAssessment(BaseModel):
    """What a reviewer may say. There is no field for run, owner, task id, workspace, permission,
    evidence authenticity or approval; `extra="forbid"` rejects any such field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation: Recommendation
    reviewed_tasks: tuple[LogicalId, ...] = Field(min_length=1, max_length=MAX_REVIEWED)
    criteria: tuple[CriterionAssessment, ...] = Field(default=(), max_length=20)
    findings: tuple[ReviewFinding, ...] = Field(default=(), max_length=20)
    confirmed_concerns: tuple[Text, ...] = Field(default=(), max_length=10)
    unresolved_concerns: tuple[Text, ...] = Field(default=(), max_length=10)
    requested_changes: tuple[Text, ...] = Field(default=(), max_length=10)


class ReviewSubmitArguments(ReviewAssessment):
    """Arguments of `review.submit`: exactly a `ReviewAssessment`."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest_of(value: Any) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def output_digest(task: AgentTask) -> str:
    """Digest of a task's stored output: what a review is bound to."""

    return digest_of(task.output if isinstance(task.output, dict) else None)


def canonical_assessment(assessment: ReviewAssessment) -> dict[str, Any]:
    data = assessment.model_dump(mode="json")
    data["reviewed_tasks"] = sorted(set(assessment.reviewed_tasks))
    return data


def contradiction(assessment: ReviewAssessment) -> str | None:
    """An approval that contradicts its own findings is refused when it is submitted."""

    if assessment.recommendation != "approve":
        return None
    if any(item.verdict == "not_met" for item in assessment.criteria):
        return "approve_with_unmet_criteria"
    if any(item.verdict == "unknown" for item in assessment.criteria):
        # An approval must not silently imply that a criterion it could not assess passed.
        return "approve_with_unknown_criteria"
    if any(item.severity in {"major", "blocking"} for item in assessment.findings):
        return "approve_with_serious_findings"
    if assessment.unresolved_concerns or assessment.requested_changes:
        return "approve_with_open_items"
    return None


@dataclass(frozen=True, slots=True)
class ShownSource:
    """A completed task the reviewer was shown (host fact from its context package)."""

    logical_id: str
    task_id: str
    cycle: int | None


class ReviewSession:
    """Trusted identity and the staging slot of exactly one reviewer task execution."""

    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        workspace_id: str | None,
        fingerprint: str | None,
        shown: Iterable[ShownSource],
    ) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.session_id = session_id
        self.workspace_id = workspace_id
        self.fingerprint = fingerprint
        by_logical: dict[str, ShownSource | None] = {}
        for source in shown:
            known = by_logical.get(source.logical_id, source)
            # The same logical id shown for two different tasks is ambiguous: it cannot be reviewed.
            by_logical[source.logical_id] = (
                source if known and known.task_id == source.task_id else None
            )
        self.shown: dict[str, ShownSource] = {k: v for k, v in by_logical.items() if v is not None}
        self._assessment: ReviewAssessment | None = None
        self._sealed = False

    @property
    def assessment(self) -> ReviewAssessment | None:
        return self._assessment

    def submit(self, assessment: ReviewAssessment) -> tuple[str, str]:
        """Stage the one assessment: `("accepted", digest)` or `("rejected", code)`."""

        if self._sealed:
            return "rejected", "session_sealed"
        reason = contradiction(assessment)
        if reason is not None:
            return "rejected", reason
        if not set(assessment.reviewed_tasks) <= set(self.shown):
            # Only work the reviewer was actually shown by the host can be reviewed.
            return "rejected", "task_not_shown"
        if len(set(assessment.reviewed_tasks)) != len(assessment.reviewed_tasks):
            return "rejected", "duplicate_tasks"
        identity = digest_of(canonical_assessment(assessment))
        if self._assessment is not None:
            same = digest_of(canonical_assessment(self._assessment)) == identity
            return ("accepted", identity) if same else ("rejected", "assessment_conflict")
        self._assessment = assessment
        return "accepted", identity

    def seal(self) -> ReviewAssessment:
        self._sealed = True
        if self._assessment is None:
            raise ReviewRecordError("no_assessment")
        return self._assessment


Authenticate = Callable[[Iterable[str], RuntimeRunRecord], Awaitable[list[str]]]
ReadTask = Callable[[str], Awaitable[AgentTask | None]]


async def build_review_record(
    session: ReviewSession,
    *,
    run: RuntimeRunRecord,
    reviewer: AgentTask,
    read_task: ReadTask,
    authenticate: Authenticate,
    cycle_of: Callable[[AgentTask], int | None],
) -> dict[str, Any]:
    """Finalize the staged assessment into the canonical record stored in the task output.

    `cycle_of` returns a task's compiler-proven cycle (or None when its identity is not proven).
    The recorded cycle is always that authoritative value. The cycle the reviewer was shown is only
    informational: absent for a same-cycle dependency, and when present it must agree.
    """

    assessment = session.seal()
    reviewed: list[dict[str, Any]] = []
    for logical in sorted(set(assessment.reviewed_tasks)):
        source = session.shown[logical]
        task = await read_task(source.task_id)
        if (
            task is None
            or task.id != source.task_id
            or task.status is not TaskStatus.COMPLETED
            or task.metadata.get("runtime_run_id") != run.id
            or (task.tenant_id, task.user_id, task.session_id)
            != (run.tenant_id, run.user_id, run.session_id)
        ):
            raise ReviewRecordError("reviewed_task_unavailable")
        cycle = cycle_of(task)
        if cycle is None:
            raise ReviewRecordError("reviewed_task_unavailable")
        if source.cycle is not None and source.cycle != cycle:
            raise ReviewRecordError("reviewed_cycle_conflict")
        if task.agent_id == reviewer.agent_id:
            raise ReviewRecordError("self_review")
        reviewed.append(
            {
                "logical_id": logical,
                "task_id": task.id,
                "cycle": cycle,
                "worker_id": task.agent_id,
                "output_digest": output_digest(task),
            }
        )
    cited = sorted({ref for finding in assessment.findings for ref in finding.evidence_refs})
    authentic = await authenticate(cited, run)
    body: dict[str, Any] = {
        "version": REVIEW_RECORD_VERSION,
        "run_id": run.id,
        "reviewer_task_id": reviewer.id,
        "reviewer_worker_id": reviewer.agent_id,
        "tenant_id": run.tenant_id,
        "user_id": run.user_id,
        "session_id": run.session_id,
        "workspace_id": session.workspace_id,
        "fingerprint": session.fingerprint,
        "assessment": canonical_assessment(assessment),
        "reviewed": reviewed,
        # Only references the host authenticated; the rest are counted, never kept.
        "evidence_refs": sorted(authentic),
        "unverified_reference_count": len(cited) - len(set(authentic)),
    }
    if len(canonical(body).encode()) > _MAX_RECORD_BYTES:
        raise ReviewRecordError("record_too_large")
    return {**body, "digest": digest_of(body)}


@dataclass(frozen=True, slots=True)
class VerifiedReview:
    """A review record that passed integrity and binding checks (not yet acceptance)."""

    task: AgentTask
    assessment: ReviewAssessment
    fingerprint: str | None
    reviewed: tuple[Mapping[str, Any], ...]
    evidence_refs: tuple[str, ...]
    digest: str


def verify_review_record(
    task: AgentTask, run: RuntimeRunRecord, *, workspace_id: str | None
) -> VerifiedReview:
    """Parse and bind the record of a stored reviewer task, or raise `ReviewRecordError`."""

    output = task.output if isinstance(task.output, dict) else None
    raw = output.get(REVIEW_OUTPUT_KEY) if output is not None else None
    if raw is None:
        raise ReviewRecordError("review_record_missing")
    if not isinstance(raw, dict) or raw.get("version") != REVIEW_RECORD_VERSION:
        raise ReviewRecordError("review_record_invalid")
    try:
        size = len(canonical(raw).encode())
    except (TypeError, ValueError):
        raise ReviewRecordError("review_record_invalid") from None
    if size > _MAX_RECORD_BYTES:
        raise ReviewRecordError("review_record_invalid")
    body = {k: v for k, v in raw.items() if k != "digest"}
    digest = raw.get("digest")
    if not isinstance(digest, str) or digest != digest_of(body):
        raise ReviewRecordError("review_digest_mismatch")
    expected = {
        "run_id": run.id,
        "reviewer_task_id": task.id,
        "reviewer_worker_id": task.agent_id,
        "tenant_id": run.tenant_id,
        "user_id": run.user_id,
        "session_id": run.session_id,
        "workspace_id": workspace_id,
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        raise ReviewRecordError("review_binding_mismatch")
    fingerprint = raw.get("fingerprint")
    if fingerprint is not None and not isinstance(fingerprint, str):
        raise ReviewRecordError("review_record_invalid")
    reviewed = raw.get("reviewed")
    evidence = raw.get("evidence_refs")
    if (
        not isinstance(reviewed, list)
        or not reviewed
        or len(reviewed) > MAX_REVIEWED
        or not all(isinstance(item, dict) for item in reviewed)
        or not isinstance(evidence, list)
        or not all(isinstance(item, str) for item in evidence)
    ):
        raise ReviewRecordError("review_record_invalid")
    try:
        assessment = ReviewAssessment.model_validate(raw.get("assessment"))
    except ValidationError:
        raise ReviewRecordError("review_record_invalid") from None
    if canonical(canonical_assessment(assessment)) != canonical(raw.get("assessment")):
        raise ReviewRecordError("review_record_invalid")
    if contradiction(assessment) is not None:
        raise ReviewRecordError("review_record_invalid")
    for item in reviewed:
        if not (
            isinstance(item.get("logical_id"), str)
            and isinstance(item.get("task_id"), str)
            and isinstance(item.get("output_digest"), str)
            and type(item.get("cycle")) is int  # the host-proven cycle, never absent or foreign
        ):
            raise ReviewRecordError("review_record_invalid")
    if sorted(str(item["logical_id"]) for item in reviewed) != sorted(
        set(assessment.reviewed_tasks)
    ):
        raise ReviewRecordError("review_record_invalid")
    return VerifiedReview(
        task=task,
        assessment=assessment,
        fingerprint=fingerprint,
        reviewed=tuple(reviewed),
        evidence_refs=tuple(evidence),
        digest=digest,
    )


class ReviewTools:
    """Builds the `review.submit` `ToolDefinition`. Holds no configuration and no authority."""

    def __init__(self) -> None:
        self._definitions = (
            ToolDefinition(
                name=REVIEW_TOOL,
                description=(
                    "Submit your one review of work you were shown: a recommendation, criteria, "
                    "findings and requested changes. It is a recommendation for the host, not an "
                    "approval, and it is recorded only when your task completes."
                ),
                input_model=ReviewSubmitArguments,
                handler=self._submit,
                permissions=frozenset({REVIEW_PERMISSION}),
                side_effect="none",
            ),
        )

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    async def _submit(self, context: AgentContext, arguments: BaseModel) -> dict[str, Any]:
        session = context.services.get(REVIEW_SESSION_SERVICE)
        if (
            not isinstance(session, ReviewSession)
            or context.tenant_id != session.tenant_id
            or context.user_id != session.user_id
            or context.session_id != session.session_id
            or context.metadata.get("runtime_run_id") != session.run_id
        ):
            raise ReviewRecordError("session_identity_mismatch")
        assert isinstance(arguments, ReviewSubmitArguments)
        assessment = ReviewAssessment.model_validate(arguments.model_dump())
        status, detail = session.submit(assessment)
        if status != "accepted":
            return {"status": "rejected", "code": detail, "recorded": False}
        # Recorded only when this reviewer task completes; not an approval of anything.
        return {"status": "staged", "assessment_digest": detail, "recorded": False}
