"""M6.3.3-A review R1: Lead view resource bounds, sanitized failures, reference availability.

A custom `TaskRunQuery` may violate its contract, a store may fail with a message that holds a
secret, and a verifier may be missing. The Lead view must stay bounded, fail closed with stable
non-sensitive codes, never log raw exception text, and never present "could not check" as
"checked and empty". Scripted stores stop a runaway loop with a flag after a call cap, because a
non-yielding loop would starve the event loop and defeat any timeout.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kaiwen_agent.artifacts import ArtifactManager
from kaiwen_agent.exceptions import ActionLookupIncompleteError
from kaiwen_agent.tasks import AgentTask, TaskRunPage, TaskStatus
from kaiwen_agent.workflow import LeadView, LeadViewAssembler, LeadViewError, TaskContextAssembler
from tests.test_agent_runtime_worker import owner
from tests.test_lead_view import (
    Lead,
    approval,
    artifact_context,
    create_evidence,
    intent,
    run_async,
)
from tests.test_task_run_query import make_task
from tests.test_workflow_fresh_evidence import SIGNER

SECRET = "sk-live-SECRET-TOKEN password=hunter2"
LOGGER = "kaiwen_agent.workflow.lead_view"
CALL_CAP = 40


class Scripted:
    """A custom `TaskRunQuery`: `respond(call_number, kwargs)` decides each page."""

    def __init__(self, respond: Callable[[int, dict[str, Any]], Any]) -> None:
        self.respond = respond
        self.calls: list[dict[str, Any]] = []
        self.runaway = False

    async def list_run_tasks(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if len(self.calls) > CALL_CAP:
            self.runaway = True
            raise AssertionError("runaway pagination")
        return self.respond(len(self.calls), kwargs)


def task_page(
    run_id: str, *names: str, cursor: str | None = None, unreadable: int = 0
) -> TaskRunPage:
    tasks = tuple(make_task(run_id, name) for name in names)
    return TaskRunPage(tasks=tasks, next_after_id=cursor, unreadable=unreadable)


async def assemble_with(lead: Lead, store: Any, run: Any, **options: Any) -> LeadView | None:
    assembler = LeadViewAssembler(runs=lead.flow.runs, tasks=store, **options)
    return await assembler.assemble(run_id=run.id, identity=owner())


async def refused(lead: Lead, store: Any, run: Any, code: str, **options: Any) -> None:
    with pytest.raises(LeadViewError) as error:
        await assemble_with(lead, store, run, **options)
    assert error.value.code == code
    assert not getattr(store, "runaway", False)


# ------------------------------------------------------------------ 1. pagination bounds


def test_an_empty_page_with_a_cursor_is_refused_not_looped_on(tmp_path: Path) -> None:
    async def scenario() -> None:
        lead = Lead(tmp_path)
        run = await lead.flow.new_run()
        store = Scripted(lambda n, kw: task_page(run.id, cursor="task_x"))
        await refused(lead, store, run, "task_listing_invalid")
        assert len(store.calls) <= 2

    run_async(scenario)


def test_a_cursor_that_does_not_advance_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        lead = Lead(tmp_path)
        run = await lead.flow.new_run()

        def respond(n: int, kw: dict[str, Any]) -> TaskRunPage:
            if n == 1:
                return task_page(run.id, "a", "b", cursor="task_b")
            return task_page(run.id, "c", cursor="task_b")  # same cursor again

        store = Scripted(respond)
        await refused(lead, store, run, "task_listing_invalid")
        assert len(store.calls) == 2

    run_async(scenario)


def test_repeated_or_out_of_order_tasks_are_refused_instead_of_duplicated(tmp_path: Path) -> None:
    async def scenario() -> None:
        lead = Lead(tmp_path)
        run = await lead.flow.new_run()
        pages = {
            "repeat_after_cursor": lambda n, kw: (
                task_page(run.id, "a", "b", cursor="task_b")
                if n == 1
                else task_page(run.id, "a", "b", cursor="task_z")
            ),
            "descending": lambda n, kw: task_page(run.id, "b", "a"),
            "duplicate_in_page": lambda n, kw: task_page(run.id, "a", "a"),
            "cursor_behind_tasks": lambda n, kw: task_page(run.id, "a", "b", cursor="task_a"),
        }
        for name, respond in pages.items():
            store = Scripted(respond)
            await refused(lead, store, run, "task_listing_invalid")
            assert len(store.calls) <= 2, name

    run_async(scenario)


def test_a_store_returning_more_tasks_than_requested_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        lead = Lead(tmp_path)
        run = await lead.flow.new_run()
        store = Scripted(lambda n, kw: task_page(run.id, "a", "b", "c", "d", "e"))
        await refused(lead, store, run, "task_listing_invalid", max_tasks=3)
        assert store.calls[0]["limit"] == 3
        # Tasks plus unreadable rows together may not exceed the request either.
        mixed = Scripted(lambda n, kw: task_page(run.id, "a", "b", "c", unreadable=1))
        await refused(lead, mixed, run, "task_listing_invalid", max_tasks=3)

    run_async(scenario)


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(lambda run_id: None, id="none"),
        pytest.param(lambda run_id: "page", id="string"),
        pytest.param(lambda run_id: object(), id="object"),
        pytest.param(lambda run_id: TaskRunPage(tasks=("not a task",)), id="non-task-item"),  # type: ignore[arg-type]
        pytest.param(lambda run_id: TaskRunPage(tasks=None), id="tasks-none"),  # type: ignore[arg-type]
        pytest.param(lambda run_id: TaskRunPage(tasks=(), next_after_id=5), id="int-cursor"),  # type: ignore[arg-type]
        pytest.param(lambda run_id: TaskRunPage(tasks=(), next_after_id=""), id="empty-cursor"),
        pytest.param(lambda run_id: TaskRunPage(tasks=(), unreadable=-1), id="negative"),
        pytest.param(lambda run_id: TaskRunPage(tasks=(), unreadable="3"), id="str-count"),  # type: ignore[arg-type]
        pytest.param(lambda run_id: TaskRunPage(tasks=(), unreadable=True), id="bool-count"),
        pytest.param(lambda run_id: TaskRunPage(tasks=(), unreadable=10**9), id="huge-count"),
    ],
)
def test_malformed_pagination_responses_are_refused(
    tmp_path: Path, response: Callable[[str], Any]
) -> None:
    async def scenario() -> None:
        lead = Lead(tmp_path)
        run = await lead.flow.new_run()
        store = Scripted(lambda n, kw: response(run.id))
        await refused(lead, store, run, "task_listing_invalid")
        assert len(store.calls) == 1

    run_async(scenario)


def test_unreadable_rows_count_toward_the_bound_and_never_extend_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        lead = Lead(tmp_path)
        run = await lead.flow.new_run()

        def respond(n: int, kw: dict[str, Any]) -> TaskRunPage:
            # Every page claims it examined only unreadable rows and that more exist.
            return TaskRunPage(tasks=(), next_after_id=f"task_{n:04d}", unreadable=kw["limit"])

        store = Scripted(respond)
        view = await assemble_with(lead, store, run, max_tasks=10)
        assert view is not None and not store.runaway
        assert view.tasks == () and view.unreadable_task_count == 10 and view.tasks_truncated
        assert len(store.calls) == 1 and store.calls[0]["limit"] == 10
        # Small pages still stop at the bound: many calls, never more than max_tasks rows.
        slow = Scripted(
            lambda n, kw: TaskRunPage(tasks=(), next_after_id=f"task_{n:04d}", unreadable=1)
        )
        view = await assemble_with(lead, slow, run, max_tasks=7)
        assert view is not None and not slow.runaway
        assert view.unreadable_task_count == 7 and len(slow.calls) == 7 and view.tasks_truncated

    run_async(scenario)


def test_the_number_of_store_calls_is_bounded_by_max_tasks(tmp_path: Path) -> None:
    async def scenario() -> None:
        lead = Lead(tmp_path)
        run = await lead.flow.new_run()
        names = [f"t{index:03d}" for index in range(30)]

        def respond(n: int, kw: dict[str, Any]) -> TaskRunPage:
            index = n - 1  # one task per page, always advancing, always more
            return task_page(run.id, names[index], cursor=f"task_{names[index]}")

        store = Scripted(respond)
        view = await assemble_with(lead, store, run, max_tasks=12)
        assert view is not None and not store.runaway
        assert len(view.tasks) == 12 and view.tasks_truncated and len(store.calls) == 12

    run_async(scenario)


def test_normal_sqlite_pagination_is_unchanged_including_unreadable_rows(tmp_path: Path) -> None:
    async def scenario() -> None:
        lead = Lead(tmp_path)
        run = await lead.flow.new_run()
        for index in range(10):
            await lead.save(run, f"t{index:02d}")
        with sqlite3.connect(lead.store.path) as connection:
            for name in ("t00", "t01"):
                connection.execute(
                    "UPDATE tasks SET task_json = '{not json' WHERE id = ?", (f"task_{name}",)
                )
        view = await lead.view(run, max_tasks=100)
        assert [t.logical_task_id for t in view.tasks] == [f"t{i:02d}" for i in range(2, 10)]
        assert view.unreadable_task_count == 2 and not view.tasks_truncated
        capped = await lead.view(run, max_tasks=4)
        assert len(capped.tasks) + capped.unreadable_task_count == 4 and capped.tasks_truncated
        exact = await lead.view(run, max_tasks=10)
        assert len(exact.tasks) + exact.unreadable_task_count == 10 and not exact.tasks_truncated

    run_async(scenario)


# ------------------------------------------------------------------ 2. sanitized failures


def no_secret_in_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    records = [record for record in caplog.records if record.name == LOGGER]
    for record in records:
        assert "SECRET" not in record.getMessage() and "hunter2" not in record.getMessage()
        assert record.exc_info is None and record.exc_text is None
    assert "SECRET" not in caplog.text and "hunter2" not in caplog.text
    return records


def assert_clean(error: BaseException, code: str) -> None:
    assert isinstance(error, LeadViewError) and error.code == code
    assert SECRET not in str(error) and "hunter2" not in repr(error)
    assert error.__cause__ is None and error.__context__ is None  # the raw error is not chained


def test_a_failing_run_store_surfaces_a_stable_code_and_no_raw_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenRuns:
        async def get(self, **_: Any) -> Any:
            raise RuntimeError(SECRET)

    async def scenario() -> None:
        lead = Lead(tmp_path)
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        assembler = LeadViewAssembler(runs=BrokenRuns(), tasks=lead.store)  # type: ignore[arg-type]
        with pytest.raises(LeadViewError) as error:
            await assembler.assemble(run_id="runtime_x", identity=owner())
        assert_clean(error.value, "run_store_unavailable")

    run_async(scenario)
    records = no_secret_in_logs(caplog)
    assert any("RuntimeError" in record.getMessage() for record in records)  # useful, sanitized


def test_a_failing_task_store_fails_closed_without_a_partial_view(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        lead = Lead(tmp_path)
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        run = await lead.flow.new_run()
        first = Scripted(lambda n, kw: (_ for _ in ()).throw(RuntimeError(SECRET)))
        with pytest.raises(LeadViewError) as error:
            await assemble_with(lead, first, run)
        assert_clean(error.value, "task_store_unavailable")

        def second_page_fails(n: int, kw: dict[str, Any]) -> TaskRunPage:
            if n == 1:
                return task_page(run.id, "a", cursor="task_a")
            raise RuntimeError(SECRET)

        with pytest.raises(LeadViewError) as later:
            await assemble_with(lead, Scripted(second_page_fails), run)
        assert_clean(later.value, "task_store_unavailable")

    run_async(scenario)
    records = no_secret_in_logs(caplog)
    assert len([r for r in records if "task_store_unavailable" in r.getMessage()]) == 2


def test_cancellation_is_never_swallowed_by_the_failure_handling(tmp_path: Path) -> None:
    async def scenario() -> None:
        lead = Lead(tmp_path)
        run = await lead.flow.new_run()
        store = Scripted(lambda n, kw: (_ for _ in ()).throw(asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            await assemble_with(lead, store, run)

    run_async(scenario)


def test_a_failing_artifact_store_is_reported_unavailable_not_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenArtifacts(ArtifactManager):
        async def read(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(SECRET)

    async def scenario() -> None:
        lead = Lead(tmp_path)
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        run = await lead.flow.new_run()
        task = make_task(run.id, "worker")
        task.output = {"artifact_refs": ["artifact_1"], "evidence_refs": ["artifact_2"]}
        await lead.store.save_task(task)
        broken = TaskContextAssembler(
            store=lead.store, artifacts=BrokenArtifacts(), evidence_signer=SIGNER
        )
        view = await lead.view(run, context=broken)
        (item,) = view.tasks
        assert item.artifact_check == "unavailable" and item.evidence_check == "unavailable"
        assert item.artifact_refs == () and item.authenticated_evidence_refs == ()
        assert item.unverified_reference_count == 0 and item.unchecked_reference_count == 2
        assert "artifacts:store_error" in view.unavailable_sources
        assert SECRET not in view.model_dump_json() and "hunter2" not in view.model_dump_json()

    run_async(scenario)
    records = no_secret_in_logs(caplog)
    assert any("artifacts:store_error" in record.getMessage() for record in records)


def test_failing_or_incomplete_approval_sources_are_unavailable_and_sanitized(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenActions:
        is_durable = True

        async def list_run_action_intents(self, **_: Any) -> Any:
            raise RuntimeError(SECRET)

    class IncompleteActions:
        is_durable = True

        async def list_run_action_intents(self, **_: Any) -> Any:
            raise ActionLookupIncompleteError(SECRET)

    class BrokenApprovals:
        is_durable = True

        async def get_approval_for_intent(self, **_: Any) -> Any:
            raise RuntimeError(SECRET)

    async def scenario() -> None:
        lead = Lead(tmp_path)
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        run = await lead.flow.new_run()
        await lead.save(run, "a")
        record = intent(run.id, "pending")
        await lead.store.prepare_action_intent(record)
        await lead.store.create_pending_approval(approval(record))

        broken = await lead.view(run, actions=BrokenActions(), approvals=lead.store)
        assert broken.pending_approvals is None
        assert "approvals:store_error" in broken.unavailable_sources
        incomplete = await lead.view(run, actions=IncompleteActions(), approvals=lead.store)
        assert incomplete.pending_approvals is None
        assert "approvals:incomplete" in incomplete.unavailable_sources
        half = await lead.view(run, actions=lead.store, approvals=BrokenApprovals())
        assert half.pending_approvals is None  # never a partial list presented as complete
        assert "approvals:store_error" in half.unavailable_sources
        missing = await lead.view(run)
        assert missing.pending_approvals is None
        assert "approvals:not_configured" in missing.unavailable_sources
        ok = await lead.view(run, actions=lead.store, approvals=lead.store)
        assert ok.pending_approvals is not None and len(ok.pending_approvals) == 1
        assert not any(s.startswith("approvals:") for s in ok.unavailable_sources)

    run_async(scenario)
    no_secret_in_logs(caplog)


def test_unavailable_source_codes_are_stable_and_non_sensitive(tmp_path: Path) -> None:
    async def scenario() -> None:
        lead = Lead(tmp_path)
        run = await lead.flow.new_run()
        await lead.save(run, "a")
        view = await lead.view(run, context=None)
        assert view.unavailable_sources
        for code in view.unavailable_sources:
            assert re.fullmatch(r"[a-z]+:[a-z_]+", code), code

    run_async(scenario)


# ------------------------------------------------------------------ 4. reference availability


async def task_with_claims(lead: Lead, run: Any, good: str) -> None:
    task = make_task(run.id, "worker")
    task.status = TaskStatus.COMPLETED
    task.output = {"artifact_refs": [good], "evidence_refs": [good]}
    await lead.store.save_task(task)


def test_reference_availability_distinguishes_unchecked_from_checked_and_empty(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        lead = Lead(tmp_path)
        run = await lead.flow.new_run()
        good = await create_evidence(lead.artifacts, artifact_context(run.id))
        await task_with_claims(lead, run, good)

        def checks(view: LeadView) -> tuple[str, str, int, int]:
            (item,) = view.tasks
            return (
                item.artifact_check,
                item.evidence_check,
                item.unverified_reference_count,
                item.unchecked_reference_count,
            )

        # 1. No assembler at all: nothing was checked, nothing is "dropped".
        bare = LeadViewAssembler(runs=lead.flow.runs, tasks=lead.store)
        view = await bare.assemble(run_id=run.id, identity=owner())
        assert view is not None
        assert checks(view) == ("unavailable", "unavailable", 0, 2)
        assert {"artifacts:not_configured", "evidence:not_configured"} <= set(
            view.unavailable_sources
        )
        assert view.tasks[0].artifact_refs == () and view.tasks[0].authenticated_evidence_refs == ()

        # 2. An assembler without an artifact store cannot check either kind.
        no_artifacts = TaskContextAssembler(store=lead.store, evidence_signer=SIGNER)
        view = await lead.view(run, context=no_artifacts)
        assert checks(view) == ("unavailable", "unavailable", 0, 2)
        assert "artifacts:not_configured" in view.unavailable_sources

        # 3. Artifacts without a signer: artifacts are checked, evidence is not.
        no_signer = TaskContextAssembler(store=lead.store, artifacts=lead.artifacts)
        view = await lead.view(run, context=no_signer)
        assert checks(view) == ("checked", "unavailable", 0, 1)
        assert view.tasks[0].artifact_refs == (good,)
        assert view.tasks[0].authenticated_evidence_refs == ()
        assert "evidence:no_signer" in view.unavailable_sources
        assert "artifacts:not_configured" not in view.unavailable_sources

        # 4. Full verification: checked, and a failed check is "dropped", not "unchecked".
        view = await lead.view(run)
        assert checks(view) == ("checked", "checked", 0, 0)
        assert view.tasks[0].authenticated_evidence_refs == (good,)
        assert not any(
            s.split(":")[0] in {"artifacts", "evidence"} for s in view.unavailable_sources
        )
        forged = make_task(run.id, "forger")
        forged.output = {"evidence_refs": ["artifact_missing"], "artifact_refs": []}
        await lead.store.save_task(forged)
        view = await lead.view(run)
        by_name = {t.logical_task_id: t for t in view.tasks}
        assert by_name["forger"].evidence_check == "checked"
        assert by_name["forger"].unverified_reference_count == 1
        assert by_name["forger"].unchecked_reference_count == 0

    run_async(scenario)


def test_a_task_with_no_claims_is_checked_and_empty_only_when_checking_was_possible(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        lead = Lead(tmp_path)
        run = await lead.flow.new_run()
        await lead.save(run, "quiet")
        (checked,) = (await lead.view(run)).tasks
        assert (checked.artifact_check, checked.evidence_check) == ("checked", "checked")
        bare = LeadViewAssembler(runs=lead.flow.runs, tasks=lead.store)
        view = await bare.assemble(run_id=run.id, identity=owner())
        assert view is not None
        # No claims and no verifier: still "unavailable", never an implied verified-empty.
        assert (view.tasks[0].artifact_check, view.tasks[0].evidence_check) == (
            "unavailable",
            "unavailable",
        )
        assert view.tasks[0].unchecked_reference_count == 0

    run_async(scenario)


# ------------------------------------------------------------------ 3. task provenance boundary


def test_the_membership_check_does_not_prove_which_component_wrote_the_task(
    tmp_path: Path,
) -> None:
    """Pins a documented limit (ADR 0015): ownership is verified membership, not write origin.

    A host component with `save_task` access that claims an existing run of the same owner is
    accepted. That is the trusted-write boundary: `TaskStore` is a host-controlled interface.
    """

    async def scenario() -> None:
        lead = Lead(tmp_path)
        mine = await lead.flow.new_run()
        claimed = make_task(mine.id, "hand_written")  # not produced by WorkflowCompiler
        await lead.store.save_task(claimed)
        assert [t.logical_task_id for t in (await lead.view(mine)).tasks] == ["hand_written"]
        # What the check does establish: another owner, a missing run, another session and a
        # claim made only in `input` are all refused.
        other = await lead.flow.new_run(user="user-2")
        await lead.store.save_task(make_task(other.id, "cross_owner"))  # claims user-1's fields
        assert [t.logical_task_id for t in (await lead.view(mine)).tasks] == ["hand_written"]
        page = await lead.store.list_run_tasks(
            tenant_id="tenant-1", user_id="user-2", runtime_run_id=other.id
        )
        assert page.tasks == ()

    run_async(scenario)


def test_only_the_workflow_compiler_writes_a_run_id_into_task_metadata() -> None:
    """The in-package entry points that accept untrusted input never set `runtime_run_id`."""

    root = Path(__file__).resolve().parents[1] / "src" / "kaiwen_agent"
    pattern = re.compile(r"""["']runtime_run_id["']\s*:""")
    writers = sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    )
    assert writers == ["workflow/compiler.py"]
    assert AgentTask.model_fields["metadata"].annotation == dict[str, Any]
