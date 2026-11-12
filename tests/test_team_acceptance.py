"""M6.3.4: the host-controlled team acceptance decision over stored reviewer records.

Real SQLite task storage and the existing artifact manager and evidence signer; stored tasks carry
compiler-derived ids. Each adversarial case forges or removes exactly one fact. Nothing here is a
model. What is pinned: acceptance needs every configured mandatory condition, recomputed from
storage; the completion gate keeps its own authority; a reviewer's word grants nothing; and any
fact the host cannot establish is `unavailable`, never success.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from kaiwen_agent.quality import CompletionGateResult, CompletionGateStatus
from kaiwen_agent.runtime import RuntimeRunRecord, WorkspaceBinding
from kaiwen_agent.tasks import AgentTask, TaskRunPage, TaskStatus
from kaiwen_agent.workflow import (
    HumanApproval,
    HumanApprovalStatus,
    ReviewAssessment,
    ReviewRecordError,
    TaskContextAssembler,
    TeamAcceptance,
    TeamAcceptancePolicy,
    TeamAcceptanceStatus,
)
from kaiwen_agent.workflow import review as rv
from kaiwen_agent.workflow.compiler import compiled_task_id
from tests.test_agent_runtime_worker import _artifact_context, _evidence, run_async
from tests.test_cross_cycle_handoffs import Env, Proxy
from tests.test_workflow_fresh_evidence import SIGNER

PASS_GATE = CompletionGateResult(status=CompletionGateStatus.PASS, verifications=())
FP = "fingerprint-current"
BINDING = WorkspaceBinding(workspace_id="ws-1", root="main", policy_checksum="p1")
Status = TeamAcceptanceStatus


def assessment(
    recommendation: str = "approve", reviewed: tuple[str, ...] = ("code", "test"), **fields: Any
) -> ReviewAssessment:
    return ReviewAssessment(recommendation=recommendation, reviewed_tasks=reviewed, **fields)


class Team:
    """A stored delivery: code and test (cycle 2) and one reviewer (cycle 3) with a record."""

    def __init__(self, env: Env, run: RuntimeRunRecord) -> None:
        self.env = env
        self.run = run
        self.tasks: dict[str, AgentTask] = {}
        self.reviewers: list[AgentTask] = []
        self.evidence = ""

    @classmethod
    async def build(cls, env: Env, run: RuntimeRunRecord, **options: Any) -> Team:
        team = cls(env, run)
        team.evidence = await _evidence(env, _artifact_context(run.id))
        lead = await env.seed(run, 1, "lead", agent="lead")
        team.tasks["lead"] = lead
        team.tasks["code"] = await env.seed(
            run, 2, "code", parent=lead, output={"summary": "implemented", "verified": True}
        )
        team.tasks["test"] = await env.seed(
            run,
            2,
            "test",
            parent=lead,
            agent="tester",
            output={"summary": "tests passed", "evidence_refs": [team.evidence]},
        )
        team.tasks["plan2"] = await env.seed(run, 2, "plan2", parent=lead, agent="lead2")
        if options.pop("with_reviewer", True):
            await team.add_reviewer(**options)
        return team

    def record(
        self,
        reviewer_id: str,
        *,
        agent: str = "reviewer",
        recommendation: str = "approve",
        reviewed: tuple[str, ...] = ("code", "test"),
        fingerprint: str | None = FP,
        evidence: tuple[str, ...] | None = None,
        mutate: Callable[[dict[str, Any]], None] | None = None,
        redigest: bool = True,
        workspace: str | None = None,
        assessment_fields: dict[str, Any] | None = None,
        digests: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        run = self.run
        body: dict[str, Any] = {
            "version": rv.REVIEW_RECORD_VERSION,
            "run_id": run.id,
            "reviewer_task_id": reviewer_id,
            "reviewer_worker_id": agent,
            "tenant_id": run.tenant_id,
            "user_id": run.user_id,
            "session_id": run.session_id,
            "workspace_id": workspace,
            "fingerprint": fingerprint,
            "assessment": rv.canonical_assessment(
                assessment(recommendation, reviewed, **(assessment_fields or {}))
            ),
            "reviewed": [
                {
                    "logical_id": name,
                    "task_id": self.tasks[name].id,
                    "cycle": 2,
                    "worker_id": self.tasks[name].agent_id,
                    "output_digest": (digests or {}).get(name, rv.output_digest(self.tasks[name])),
                }
                for name in sorted(reviewed)
            ],
            "evidence_refs": list(evidence if evidence is not None else (self.evidence,)),
            "unverified_reference_count": 0,
        }
        if mutate is not None:
            mutate(body)
        return {**body, "digest": rv.digest_of(body) if redigest else "0" * 64}

    async def add_reviewer(
        self,
        logical: str = "review",
        *,
        agent: str = "reviewer",
        cycle: int = 3,
        status: TaskStatus = TaskStatus.COMPLETED,
        output: Any = ...,
        **record: Any,
    ) -> AgentTask:
        reviewer_id = compiled_task_id(self.run.id, cycle, logical)
        workspace = self.run.workspace_binding.workspace_id if self.run.workspace_binding else None
        record.setdefault("workspace", workspace)
        body = {rv.REVIEW_OUTPUT_KEY: self.record(reviewer_id, agent=agent, **record)}
        task = await self.env.seed(
            self.run,
            cycle,
            logical,
            parent=self.tasks["plan2"],
            agent=agent,
            status=status,
            output=body if output is ... else output,
        )
        self.reviewers.append(task)
        return task

    def acceptance(self, **policy: Any) -> TeamAcceptance:
        settings: dict[str, Any] = {
            "reviewer_workers": {"reviewer": "reviewer"},
            "required_tasks": frozenset({"code", "test"}),
        }
        settings.update(policy)
        assembler = TaskContextAssembler(
            store=self.env.store, artifacts=self.env.artifacts, evidence_signer=SIGNER
        )
        return TeamAcceptance(
            policy=TeamAcceptancePolicy(**settings),
            tasks=self.env.store,
            runs=self.env.runs,
            assembler=assembler,
        )

    async def decide(
        self, *, gate: CompletionGateResult = PASS_GATE, fingerprint: str | None = FP, **policy: Any
    ) -> Any:
        return await self.acceptance(**policy).evaluate(
            run=self.run, gate=gate, fingerprint=fingerprint
        )


async def team(tmp_path: Path, *, binding: WorkspaceBinding | None = None, **options: Any) -> Team:
    env = Env(tmp_path, binding=binding)
    run = await env.new_run()
    return await Team.build(env, run, **options)


def expect(decision: Any, status: TeamAcceptanceStatus, code: str) -> None:
    assert (decision.status, decision.code) == (status, code), decision
    assert decision.accepted is (status is Status.ACCEPTED)


# ----------------------------------------------------------------------------- the happy path


def test_a_complete_delivery_is_accepted_and_the_decision_names_what_it_used(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        decision = await t.decide()
        expect(decision, Status.ACCEPTED, "accepted")
        assert decision.reviewer_task_ids == (t.reviewers[0].id,)

    run_async(scenario)


def test_the_decision_is_recomputed_from_storage_and_changes_when_storage_changes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        expect(await t.decide(), Status.ACCEPTED, "accepted")
        # Nothing was remembered: change a reviewed output and the same call answers differently.
        changed = t.tasks["code"].model_copy(update={"output": {"summary": "rewritten"}})
        await t.env.store.save_task(changed)
        expect(await t.decide(), Status.CHANGES_REQUIRED, "reviewed_work_changed")

    run_async(scenario)


# -------------------------------------------------------------------- the gate keeps its authority


@pytest.mark.parametrize(
    "gate_status",
    [
        CompletionGateStatus.BLOCKED,
        CompletionGateStatus.APPROVAL_REQUIRED,
        CompletionGateStatus.CONFIGURATION_ERROR,
    ],
)
def test_an_approving_review_never_overrides_a_gate_that_did_not_pass(
    tmp_path: Path, gate_status: CompletionGateStatus
) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        gate = CompletionGateResult(status=gate_status, verifications=())
        expect(await t.decide(gate=gate), Status.BLOCKED, "gate_not_passed")

    run_async(scenario)


# ------------------------------------------------------------------------ reviewer identity


def test_a_missing_reviewer_is_not_an_approving_reviewer(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        expect(await t.decide(), Status.BLOCKED, "review_missing")

    run_async(scenario)


def test_a_worker_that_only_claims_to_be_a_reviewer_is_not_one(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        # A valid-looking record on a task of an unlisted worker: its agent_id says reviewer-ish.
        await t.add_reviewer("sneaky", agent="coder", output=...)
        t.reviewers.clear()
        expect(await t.decide(), Status.BLOCKED, "review_missing")
        # The record's own claim of the worker cannot make it one either.
        expect(
            await t.decide(reviewer_workers={"auditor": "reviewer"}),
            Status.BLOCKED,
            "review_missing",
        )

    run_async(scenario)


def test_a_reviewer_task_that_did_not_complete_blocks(tmp_path: Path) -> None:
    async def scenario() -> None:
        for status in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.RUNNING):
            t = await team(tmp_path / status.value, with_reviewer=False)
            await t.add_reviewer(status=status, output=None)
            expect(await t.decide(), Status.BLOCKED, "review_not_completed")

    run_async(scenario)


def test_min_reviewers_is_enforced(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        expect(await t.decide(min_reviewers=2), Status.BLOCKED, "review_missing")
        await t.add_reviewer("review_b")
        expect(await t.decide(min_reviewers=2), Status.ACCEPTED, "accepted")

    run_async(scenario)


def test_two_reviewers_must_agree_and_a_block_wins(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        await t.add_reviewer(
            "review_b",
            recommendation="changes_requested",
            assessment_fields={"requested_changes": ["rename it"]},
        )
        expect(await t.decide(), Status.CHANGES_REQUIRED, "reviewer_requested_changes")
        await t.add_reviewer("review_c", recommendation="blocked")
        expect(await t.decide(), Status.BLOCKED, "reviewer_blocked")

    run_async(scenario)


def test_only_the_newest_review_cycle_counts(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        await t.add_reviewer(
            "old",
            cycle=3,
            recommendation="changes_requested",
            assessment_fields={"requested_changes": ["x"]},
        )
        expect(await t.decide(), Status.CHANGES_REQUIRED, "reviewer_requested_changes")
        await t.add_reviewer("new", cycle=4)  # a later review supersedes the earlier one
        expect(await t.decide(), Status.ACCEPTED, "accepted")

    run_async(scenario)


# ------------------------------------------------------------- records: integrity and binding

RECORD_FAILURES: list[tuple[str, dict[str, Any], str]] = [
    ("digest", {"redigest": False}, "review_digest_mismatch"),
    ("run", {"mutate": lambda b: b.update(run_id="other-run")}, "review_binding_mismatch"),
    ("tenant", {"mutate": lambda b: b.update(tenant_id="tenant-2")}, "review_binding_mismatch"),
    ("user", {"mutate": lambda b: b.update(user_id="user-9")}, "review_binding_mismatch"),
    ("session", {"mutate": lambda b: b.update(session_id="s-2")}, "review_binding_mismatch"),
    ("workspace", {"workspace": "ws-other"}, "review_binding_mismatch"),
    (
        "reviewer_task",
        {"mutate": lambda b: b.update(reviewer_task_id="x")},
        "review_binding_mismatch",
    ),
    (
        "worker",
        {"mutate": lambda b: b.update(reviewer_worker_id="coder")},
        "review_binding_mismatch",
    ),
    ("version", {"mutate": lambda b: b.update(version=9)}, "review_record_invalid"),
    (
        "assessment",
        {"mutate": lambda b: b.update(assessment={"recommendation": "yes"})},
        "review_record_invalid",
    ),
    (
        "extra_assessment_field",
        {"mutate": lambda b: b["assessment"].update(approved=True)},
        "review_record_invalid",
    ),
    ("reviewed_empty", {"mutate": lambda b: b.update(reviewed=[])}, "review_record_invalid"),
    ("reviewed_shape", {"mutate": lambda b: b.update(reviewed=["x"])}, "review_record_invalid"),
    (
        "evidence_shape",
        {"mutate": lambda b: b.update(evidence_refs="nope")},
        "review_record_invalid",
    ),
    ("fingerprint_type", {"mutate": lambda b: b.update(fingerprint=7)}, "review_record_invalid"),
    ("reviewed_mismatch", {"mutate": lambda b: b["reviewed"].pop()}, "review_record_invalid"),
    (
        "cycle_missing",
        {"mutate": lambda b: b["reviewed"][0].pop("cycle")},
        "review_record_invalid",
    ),
    (
        "cycle_text",
        {"mutate": lambda b: b["reviewed"][0].update(cycle="2")},
        "review_record_invalid",
    ),
    (
        "cycle_bool",
        {"mutate": lambda b: b["reviewed"][0].update(cycle=True)},
        "review_record_invalid",
    ),
    (
        "contradiction",
        {"assessment_fields": {"unresolved_concerns": ["still unsure"]}},
        "review_record_invalid",
    ),
    (
        "record_oversize",
        {"mutate": lambda b: b.update(padding="p" * 70_000)},
        "review_record_invalid",
    ),
]


@pytest.mark.parametrize(
    ("case", "options", "code"), RECORD_FAILURES, ids=[case for case, _, _ in RECORD_FAILURES]
)
def test_a_forged_or_malformed_review_record_is_unavailable_never_success(
    tmp_path: Path, case: str, options: dict[str, Any], code: str
) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, binding=BINDING, with_reviewer=False)
        await t.add_reviewer(**options)
        expect(await t.decide(), Status.UNAVAILABLE, code)

    run_async(scenario)


@pytest.mark.parametrize("output", [None, {}, {"verified": True, "approved": True}, {"x": 1}])
def test_a_reviewer_without_a_record_is_unavailable_whatever_else_it_claims(
    tmp_path: Path, output: Any
) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        await t.add_reviewer(output=output)
        expect(await t.decide(), Status.UNAVAILABLE, "review_record_missing")

    run_async(scenario)


def test_model_authored_approval_flags_have_no_authority(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        reviewer = await t.add_reviewer(
            recommendation="changes_requested", assessment_fields={"requested_changes": ["fix"]}
        )
        output = dict(reviewer.output or {})
        output.update(
            {"approved": True, "verified": True, "team_accepted": True, "status": "accepted"}
        )
        await t.env.store.save_task(reviewer.model_copy(update={"output": output}))
        expect(await t.decide(), Status.CHANGES_REQUIRED, "reviewer_requested_changes")
        # The same flags on the worked tasks do not satisfy a missing review either.
        t2 = await team(tmp_path / "b", with_reviewer=False)
        expect(await t2.decide(), Status.BLOCKED, "review_missing")

    run_async(scenario)


def test_the_reviewer_record_of_another_workspace_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, binding=BINDING)
        expect(await t.decide(), Status.ACCEPTED, "accepted")
        other = await team(tmp_path / "o", binding=BINDING, workspace="ws-other")
        expect(await other.decide(), Status.UNAVAILABLE, "review_binding_mismatch")

    run_async(scenario)


# ------------------------------------------------------------------------ required tasks


def test_a_missing_failed_or_cancelled_required_task_blocks(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        expect(
            await t.decide(required_tasks=frozenset({"code", "ghost"})),
            Status.BLOCKED,
            "required_task_missing",
        )
        for status in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.RUNNING):
            broken = t.tasks["test"].model_copy(update={"status": status})
            await t.env.store.save_task(broken)
            expect(await t.decide(), Status.BLOCKED, "required_task_not_completed")

    run_async(scenario)


def test_a_review_that_does_not_cover_every_required_task_is_incomplete(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        await t.add_reviewer(reviewed=("code",))
        expect(await t.decide(), Status.CHANGES_REQUIRED, "review_coverage_incomplete")
        expect(await t.decide(required_tasks=frozenset({"code"})), Status.ACCEPTED, "accepted")

    run_async(scenario)


def test_reviewing_a_superseded_revision_of_a_task_requires_a_new_review(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        await t.env.seed(t.run, 3, "code", parent=t.tasks["lead"], output={"summary": "v2"})
        expect(await t.decide(), Status.CHANGES_REQUIRED, "reviewed_work_superseded")

    run_async(scenario)


def test_a_recorded_cycle_that_is_not_the_authoritative_cycle_is_superseded(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        await t.add_reviewer(mutate=lambda b: b["reviewed"][0].update(cycle=3))
        expect(await t.decide(), Status.CHANGES_REQUIRED, "reviewed_work_superseded")

    run_async(scenario)


def test_a_review_whose_digest_names_other_output_is_stale(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        await t.add_reviewer(digests={"code": "0" * 64})
        expect(await t.decide(), Status.CHANGES_REQUIRED, "reviewed_work_changed")

    run_async(scenario)


def test_a_reviewed_task_that_is_no_longer_completed_needs_a_new_review(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        await t.env.store.save_task(
            t.tasks["code"].model_copy(update={"status": TaskStatus.FAILED})
        )
        # The required task itself is no longer completed: the stronger, earlier answer wins.
        expect(await t.decide(), Status.BLOCKED, "required_task_not_completed")
        expect(
            await t.decide(required_tasks=frozenset()),
            Status.CHANGES_REQUIRED,
            "reviewed_work_changed",
        )

    run_async(scenario)


def test_a_reviewer_cannot_review_its_own_worker_or_another_reviewer(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        expect(
            await t.decide(reviewer_workers={"reviewer": "reviewer", "coder": "reviewer"}),
            Status.BLOCKED,
            "self_review",
        )

    run_async(scenario)


# ---------------------------------------------------------------------- fingerprint freshness


def test_a_review_of_another_workspace_state_is_stale(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        expect(
            await t.decide(fingerprint="fingerprint-new"), Status.CHANGES_REQUIRED, "review_stale"
        )

    run_async(scenario)


def test_a_missing_fingerprint_fails_closed_unless_the_host_opts_out(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        expect(await t.decide(fingerprint=None), Status.UNAVAILABLE, "fingerprint_unavailable")
        loose = await team(tmp_path / "l", with_reviewer=False)
        await loose.add_reviewer(fingerprint=None)
        expect(
            await loose.decide(fingerprint=None, require_fingerprint=False),
            Status.ACCEPTED,
            "accepted",
        )

    run_async(scenario)


# -------------------------------------------------------------------------------- evidence


def test_an_approval_without_authenticated_evidence_is_not_enough(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        await t.add_reviewer(evidence=())
        expect(await t.decide(), Status.CHANGES_REQUIRED, "no_authenticated_evidence")
        expect(await t.decide(require_evidence=False), Status.ACCEPTED, "accepted")

    run_async(scenario)


@pytest.mark.parametrize("kind", ["unsigned", "foreign_run", "invalid_ref", "gone"])
def test_cited_evidence_is_authenticated_again_at_acceptance(tmp_path: Path, kind: str) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.new_run()
        other = await env.flow.new_run()
        t = await Team.build(env, run, with_reviewer=False)
        ref = {
            "unsigned": await _evidence(env, _artifact_context(run.id), signed=False),
            "foreign_run": await _evidence(env, _artifact_context(other.id)),
            "invalid_ref": "artifact_forged",
            "gone": t.evidence,
        }[kind]
        await t.add_reviewer(evidence=(ref,))
        if kind == "gone":
            env.artifacts.__init__()  # type: ignore[misc]  # an artifact store lost on restart
        expect(await t.decide(), Status.UNAVAILABLE, "evidence_unverifiable")

    run_async(scenario)


# --------------------------------------------------------------------- inventory and identity


class Listing(Proxy):
    def __init__(self, real: Any, mutate: Callable[[TaskRunPage], TaskRunPage]) -> None:
        super().__init__(real)
        self.mutate = mutate

    async def list_run_tasks(self, **options: Any) -> Any:
        return self.mutate(await self._real.list_run_tasks(**options))


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("unreadable", "inventory_incomplete"),
        ("foreign_session", "inventory_incomplete"),
        ("duplicate", "inventory_invalid"),
        ("raises", "inventory_unavailable"),
    ],
)
def test_an_incomplete_or_untrustworthy_inventory_is_unavailable(
    tmp_path: Path, case: str, code: str
) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)

        def mutate(page: TaskRunPage) -> TaskRunPage:
            if case == "unreadable":
                return TaskRunPage(tasks=page.tasks, next_after_id=None, unreadable=1)
            if case == "foreign_session":
                last = page.tasks[-1].model_copy(update={"session_id": "elsewhere"})
                return TaskRunPage(tasks=(*page.tasks[:-1], last), next_after_id=None)
            if case == "duplicate":
                return TaskRunPage(tasks=(*page.tasks, page.tasks[-1]), next_after_id=None)
            raise RuntimeError("sqlite://user:secret-token@host/db")

        acceptance = t.acceptance()
        acceptance = TeamAcceptance(
            policy=acceptance.policy,
            tasks=Listing(t.env.store, mutate),  # type: ignore[arg-type]
            runs=acceptance.runs,
            assembler=acceptance.assembler,
        )
        decision = await acceptance.evaluate(run=t.run, gate=PASS_GATE, fingerprint=FP)
        expect(decision, Status.UNAVAILABLE, code)
        assert "secret-token" not in decision.model_dump_json()

    run_async(scenario)


def test_a_bounded_scan_smaller_than_the_run_is_unavailable_not_partial(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        expect(await t.decide(max_scan_tasks=3), Status.UNAVAILABLE, "inventory_incomplete")

    run_async(scenario)


def test_a_row_whose_cycle_its_id_does_not_prove_makes_acceptance_unavailable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        await t.env.seed(t.run, 1, "ghost", parent=t.tasks["lead"], meta_cycle=2)
        expect(await t.decide(), Status.UNAVAILABLE, "task_identity_invalid")

    run_async(scenario)


def test_rows_of_another_run_owner_or_tenant_are_never_considered(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        stranger = await t.env.new_run(user="user-9")
        # A convincing foreign reviewer of another user's run, and another run of the same user.
        foreign = await Team.build(t.env, stranger)
        assert foreign.reviewers
        same_user = await t.env.flow.new_run()
        await Team.build(t.env, same_user)
        decision = await t.decide()
        expect(decision, Status.ACCEPTED, "accepted")
        assert decision.reviewer_task_ids == (t.reviewers[0].id,)

    run_async(scenario)


# ----------------------------------------------------------------------- human approval


class Human:
    """A host approval source: records what it was asked and answers as scripted."""

    def __init__(
        self, status: HumanApprovalStatus, *, echo: bool = True, fail: bool = False
    ) -> None:
        self.status = status
        self.echo = echo
        self.fail = fail
        self.subjects: list[Any] = []

    async def decision(self, subject: Any) -> HumanApproval:
        self.subjects.append(subject)
        if self.fail:
            raise RuntimeError("vault://token-in-message")
        digest = subject.digest if self.echo else "f" * 64
        return HumanApproval(status=self.status, subject_digest=digest)


def test_a_pending_human_approval_is_not_an_allow(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        human = Human(HumanApprovalStatus.PENDING)
        decision = await t.decide(human_approval=human)
        expect(decision, Status.APPROVAL_REQUIRED, "human_approval_pending")
        assert decision.subject_digest == human.subjects[0].digest

    run_async(scenario)


def test_a_denied_human_approval_blocks_and_stays_denied_for_that_exact_delivery(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        human = Human(HumanApprovalStatus.DENIED)
        expect(await t.decide(human_approval=human), Status.BLOCKED, "human_denied")
        expect(await t.decide(human_approval=human), Status.BLOCKED, "human_denied")  # replay

    run_async(scenario)


def test_an_approval_for_other_content_owner_or_state_does_not_match(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        wrong = Human(HumanApprovalStatus.APPROVED, echo=False)
        expect(await t.decide(human_approval=wrong), Status.BLOCKED, "approval_mismatch")
        ok = Human(HumanApprovalStatus.APPROVED)
        expect(await t.decide(human_approval=ok), Status.ACCEPTED, "accepted")
        # The approval granted for this delivery does not carry over once anything it covers moves.
        changed = t.tasks["code"].model_copy(update={"output": {"summary": "x"}})
        await t.env.store.save_task(changed)
        again = Human(HumanApprovalStatus.APPROVED)
        expect(
            await t.decide(human_approval=again), Status.CHANGES_REQUIRED, "reviewed_work_changed"
        )
        assert again.subjects == []

    run_async(scenario)


def test_the_approval_subject_binds_owner_run_workspace_fingerprint_and_outputs(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first = await team(tmp_path / "a", binding=BINDING)
        second = await team(tmp_path / "b", binding=BINDING)
        human_a, human_b = Human(HumanApprovalStatus.PENDING), Human(HumanApprovalStatus.PENDING)
        await first.decide(human_approval=human_a)
        await second.decide(human_approval=human_b)
        assert human_a.subjects[0].digest != human_b.subjects[0].digest  # another run
        again = Human(HumanApprovalStatus.PENDING)
        await first.decide(human_approval=again, fingerprint="fingerprint-other")
        # A stale fingerprint stops before any approval is even asked for.
        assert again.subjects == []
        subject = human_a.subjects[0]
        assert subject.run_id == first.run.id and subject.workspace_id == "ws-1"
        assert subject.fingerprint == FP and len(subject.reviewed) == 2

    run_async(scenario)


def test_an_unreachable_approval_source_is_unavailable_and_leaks_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        decision = await t.decide(human_approval=Human(HumanApprovalStatus.APPROVED, fail=True))
        expect(decision, Status.UNAVAILABLE, "approval_unavailable")
        assert "token-in-message" not in decision.model_dump_json()

    run_async(scenario)


def test_a_cancelled_approval_lookup_propagates(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path)
        entered = asyncio.Event()

        class Hung:
            async def decision(self, subject: Any) -> HumanApproval:
                entered.set()
                await asyncio.Event().wait()
                raise AssertionError

        running = asyncio.create_task(t.decide(human_approval=Hung()))
        await asyncio.wait_for(entered.wait(), timeout=10)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    run_async(scenario)


def test_human_approval_does_not_rescue_a_failed_review_condition(tmp_path: Path) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        await t.add_reviewer(
            recommendation="changes_requested", assessment_fields={"requested_changes": ["fix"]}
        )
        human = Human(HumanApprovalStatus.APPROVED)
        expect(
            await t.decide(human_approval=human),
            Status.CHANGES_REQUIRED,
            "reviewer_requested_changes",
        )
        assert human.subjects == []  # a human is only asked about a deliverable delivery

    run_async(scenario)


# ------------------------------------------------------------------ contract and policy


def test_the_policy_is_validated_at_configuration() -> None:
    with pytest.raises(ValueError):
        TeamAcceptancePolicy(reviewer_workers={})
    for options in (
        {"min_reviewers": 0},
        {"min_reviewers": 11},
        {"max_scan_tasks": 0},
        {"max_scan_tasks": 501},
    ):
        with pytest.raises(ValueError):
            TeamAcceptancePolicy(reviewer_workers={"r": "reviewer"}, **options)
    with pytest.raises(ValueError):
        TeamAcceptancePolicy(reviewer_workers={"r": ""})


def test_the_reviewer_assessment_schema_is_strict_and_bounded() -> None:
    base: dict[str, Any] = {"recommendation": "approve", "reviewed_tasks": ["code"]}
    ReviewAssessment.model_validate(base)
    forged = (
        "run_id",
        "tenant_id",
        "user_id",
        "task_id",
        "worker_id",
        "workspace_id",
        "permissions",
        "verified",
        "approved",
        "evidence_authentic",
        "fingerprint",
    )
    for name in forged:
        with pytest.raises(ValidationError):
            ReviewAssessment.model_validate({**base, name: "x"})
    bad: list[dict[str, Any]] = [
        {"recommendation": "yes"},
        {"recommendation": "APPROVE"},
        {"reviewed_tasks": []},
        {"reviewed_tasks": ["a"] * 11},
        {"reviewed_tasks": ["bad id"]},
        {"criteria": [{"criterion": "x" * 501, "verdict": "met"}]},
        {"criteria": [{"criterion": "c", "verdict": "passed"}]},
        {"findings": [{"severity": "fatal", "text": "t"}]},
        {"findings": [{"severity": "info", "text": "t", "evidence_refs": ["r" * 201]}]},
        {"findings": [{"severity": "info", "text": "t", "evidence_refs": ["bad ref"]}]},
        {"findings": [{"severity": "info", "text": "t"}] * 21},
        {"unresolved_concerns": ["c" * 501]},
        {"requested_changes": ["x"] * 11},
        {"confirmed_concerns": [""]},
    ]
    for override in bad:
        with pytest.raises(ValidationError):
            ReviewAssessment.model_validate({**base, **override})


def test_a_record_error_carries_only_a_stable_code() -> None:
    error = ReviewRecordError("review_record_missing")
    assert error.code == "review_record_missing" and str(error) == "review_record_missing"
