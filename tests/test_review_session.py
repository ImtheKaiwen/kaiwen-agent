"""M6.3.4: the reviewer session, the finalized record and the `review.submit` tool.

A reviewer can stage one bounded assessment about work the host showed it; the host binds it to the
run, owner, its own task id, the exact stored outputs and the fingerprint when the task completes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.context import AgentContext
from kaiwen_agent.runtime import RuntimeRunRecord
from kaiwen_agent.tasks import TaskStatus
from kaiwen_agent.workflow import ReviewRecordError, TaskContextAssembler
from kaiwen_agent.workflow import review as rv
from tests.test_agent_runtime_worker import run_async
from tests.test_cross_cycle_handoffs import Env
from tests.test_team_acceptance import FP, assessment, team
from tests.test_workflow_fresh_evidence import SIGNER


def session_for(run: RuntimeRunRecord, *shown: rv.ShownSource, **fields: Any) -> rv.ReviewSession:
    settings: dict[str, Any] = {
        "run_id": run.id,
        "task_id": "review-task",
        "tenant_id": run.tenant_id,
        "user_id": run.user_id,
        "session_id": run.session_id,
        "workspace_id": None,
        "fingerprint": FP,
        "shown": shown,
    }
    settings.update(fields)
    return rv.ReviewSession(**settings)


def test_a_session_accepts_one_assessment_about_work_it_was_shown(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.new_run()
        session = session_for(
            run, rv.ShownSource("code", "t-code", 2), rv.ShownSource("test", "t-test", 2)
        )
        good = assessment()
        status, digest = session.submit(good)
        assert status == "accepted"
        assert session.submit(good) == ("accepted", digest)  # identical: idempotent
        other = assessment("changes_requested", requested_changes=["x"])
        assert session.submit(other) == ("rejected", "assessment_conflict")
        assert session.assessment == good
        session.seal()
        assert session.submit(good) == ("rejected", "session_sealed")

    run_async(scenario)


def test_a_session_rejects_unshown_work_contradictions_and_duplicates(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.new_run()

        def fresh() -> rv.ReviewSession:
            return session_for(run, rv.ShownSource("code", "t-code", 2))

        assert fresh().submit(assessment(reviewed=("ghost",))) == ("rejected", "task_not_shown")
        dup = assessment(reviewed=("code", "code"))
        assert fresh().submit(dup) == ("rejected", "duplicate_tasks")
        contradictions = {
            "approve_with_unmet_criteria": {"criteria": [{"criterion": "c", "verdict": "not_met"}]},
            "approve_with_serious_findings": {"findings": [{"severity": "blocking", "text": "t"}]},
            "approve_with_open_items": {"requested_changes": ["x"]},
        }
        for code, fields in contradictions.items():
            assert fresh().submit(assessment(reviewed=("code",), **fields)) == ("rejected", code)
        # Not an approval: the same open items are fine in a changes request.
        ok = assessment("changes_requested", reviewed=("code",), requested_changes=["x"])
        assert fresh().submit(ok)[0] == "accepted"
        with pytest.raises(ReviewRecordError, match="no_assessment"):
            fresh().seal()

    run_async(scenario)


def test_a_logical_id_shown_for_two_different_tasks_cannot_be_reviewed(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.new_run()
        session = session_for(
            run,
            rv.ShownSource("code", "t1", 2),
            rv.ShownSource("code", "t2", 3),
            rv.ShownSource("test", "t3", 2),
        )
        assert set(session.shown) == {"test"}
        assert session.submit(assessment(reviewed=("code",))) == ("rejected", "task_not_shown")

    run_async(scenario)


def test_the_finalized_record_binds_host_facts_and_keeps_only_authentic_evidence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        reviewer = await t.env.seed(
            t.run,
            3,
            "review",
            parent=t.tasks["plan2"],
            agent="reviewer",
            status=TaskStatus.RUNNING,
            output=None,
        )
        shown = [rv.ShownSource(n, t.tasks[n].id, 2) for n in ("code", "test")]
        session = session_for(t.run, *shown, task_id=reviewer.id)
        refs = [t.evidence, "artifact_forged"]
        findings = [{"severity": "info", "text": "ran", "evidence_refs": refs}]
        assert session.submit(assessment(findings=findings))[0] == "accepted"
        assembler = TaskContextAssembler(
            store=t.env.store, artifacts=t.env.artifacts, evidence_signer=SIGNER
        )

        async def authenticate(refs: Any, run: Any) -> list[str]:
            return await assembler.owned_references(refs, run, authenticate=True)

        record = await rv.build_review_record(
            session,
            run=t.run,
            reviewer=reviewer,
            read_task=t.env.store.get_task,
            authenticate=authenticate,
            is_proven=lambda task: True,
        )
        assert record["evidence_refs"] == [t.evidence] and record["unverified_reference_count"] == 1
        assert record["run_id"] == t.run.id and record["reviewer_task_id"] == reviewer.id
        assert record["fingerprint"] == FP and record["workspace_id"] is None
        assert [r["output_digest"] for r in record["reviewed"]] == [
            rv.output_digest(t.tasks["code"]),
            rv.output_digest(t.tasks["test"]),
        ]
        stored = reviewer.model_copy(
            update={"output": {rv.REVIEW_OUTPUT_KEY: record}, "status": TaskStatus.COMPLETED}
        )
        assert rv.verify_review_record(stored, t.run, workspace_id=None).digest == record["digest"]
        moved = stored.model_copy(update={"id": "other"})
        with pytest.raises(ReviewRecordError, match="review_binding_mismatch"):
            rv.verify_review_record(moved, t.run, workspace_id=None)

    run_async(scenario)


@pytest.mark.parametrize("case", ["missing", "not_completed", "unproven", "self_review", "run"])
def test_a_reviewer_whose_work_cannot_be_re_read_or_proven_fails_to_finalize(
    tmp_path: Path, case: str
) -> None:
    async def scenario() -> None:
        t = await team(tmp_path, with_reviewer=False)
        agent = "coder" if case == "self_review" else "reviewer"
        reviewer = await t.env.seed(t.run, 3, "review", parent=t.tasks["plan2"], agent=agent)
        shown = rv.ShownSource("code", t.tasks["code"].id, 2)
        session = session_for(t.run, shown, task_id=reviewer.id)
        assert session.submit(assessment(reviewed=("code",)))[0] == "accepted"
        source = t.tasks["code"]
        if case == "not_completed":
            source = source.model_copy(update={"status": TaskStatus.FAILED})
        if case == "run":
            meta = {**source.metadata, "runtime_run_id": "x"}
            source = source.model_copy(update={"metadata": meta})

        async def read(task_id: str) -> Any:
            return None if case == "missing" else source

        async def authenticate(refs: Any, run: Any) -> list[str]:
            return []

        with pytest.raises(ReviewRecordError) as raised:
            await rv.build_review_record(
                session,
                run=t.run,
                reviewer=reviewer,
                read_task=read,
                authenticate=authenticate,
                is_proven=lambda task: case != "unproven",
            )
        expected = "self_review" if case == "self_review" else "reviewed_task_unavailable"
        assert raised.value.code == expected

    run_async(scenario)


def test_the_review_tool_refuses_a_session_of_another_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = Env(tmp_path)
        run = await env.new_run()
        tool = rv.ReviewTools().definitions()[0]
        session = session_for(run, rv.ShownSource("code", "t", 2))
        arguments = rv.ReviewSubmitArguments(recommendation="approve", reviewed_tasks=("code",))

        def context(**changes: Any) -> AgentContext:
            fields: dict[str, Any] = {
                "session_id": run.session_id,
                "tenant_id": run.tenant_id,
                "user_id": run.user_id,
                "metadata": {"runtime_run_id": run.id},
                "services": {rv.REVIEW_SESSION_SERVICE: session},
            }
            return AgentContext(**{**fields, **changes})

        first = await tool.handler(context(), arguments)
        assert first["status"] == "staged" and first["recorded"] is False  # never "recorded" here
        denied = (
            {"tenant_id": "x"},
            {"user_id": "x"},
            {"session_id": "x"},
            {"metadata": {"runtime_run_id": "x"}},
            {"services": {}},
        )
        for changes in denied:
            with pytest.raises(ReviewRecordError):
                await tool.handler(context(**changes), arguments)

    run_async(scenario)
