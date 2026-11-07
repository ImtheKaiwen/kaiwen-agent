"""Background run lifetime and exception observation for CodingKit (M5).

Observed before the fix: when a caller discarded the run handle, an exception raised by
the background workflow task was only reported as "Task exception was never retrieved"
at garbage-collection time. Loss of a still-running discarded task was suspected but
could not be reproduced, so the kit now holds a strong reference as hardening.
"""

from __future__ import annotations

import asyncio
import gc
import logging
from typing import Any

from kaiwen_agent.kits.coding import CodingKit
from kaiwen_agent.runtime import DurableRunManager, RuntimeRunStatus
from kaiwen_agent.types import ModelResponse
from kaiwen_agent.workers import FunctionWorker, WorkerRegistry
from tests.test_coding_simple_api import CapturingProvider, _capabilities, _execution


def _kit(tmp_path, execution) -> CodingKit:
    return CodingKit.local(
        workspace=tmp_path,
        provider="fake",
        model="test-model",
        provider_factory=lambda _: CapturingProvider([ModelResponse(text="unused")]),
        model_capabilities=_capabilities(),
        execution=execution,
    )


def test_exception_of_a_discarded_run_is_observed_and_logged(tmp_path, caplog) -> None:
    class _BrokenRuns(DurableRunManager):
        async def start(self, **kwargs: Any) -> Any:
            raise RuntimeError("store unavailable")

    async def scenario() -> list[dict[str, Any]]:
        execution = _execution(tmp_path)
        execution.controller.runs = _BrokenRuns(execution.runs.store)
        kit = _kit(tmp_path, execution)
        unobserved: list[dict[str, Any]] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: unobserved.append(context)
        )
        handle = await kit.agent().start("Complete one bounded task")
        del handle  # the caller keeps no reference
        await asyncio.sleep(0.2)
        gc.collect()
        await asyncio.sleep(0.05)
        return unobserved

    with caplog.at_level(logging.WARNING, logger="kaiwen_agent.kits.coding"):
        unobserved = asyncio.run(scenario())

    assert unobserved == [], "background task exception was never retrieved"
    assert any("RuntimeError" in record.getMessage() for record in caplog.records)


def test_a_discarded_handle_does_not_lose_the_running_workflow(tmp_path) -> None:
    async def scenario() -> None:
        execution = _execution(tmp_path)
        release = asyncio.Event()
        workers = WorkerRegistry()

        async def wait_for_release(task: Any, context: Any) -> dict[str, Any]:
            await release.wait()
            return {"ok": True}

        workers.register("worker", FunctionWorker(wait_for_release))
        execution.controller.compiler.workers = workers
        execution.controller.scheduler_factory = lambda graph: _scheduler(graph, workers, execution)
        kit = _kit(tmp_path, execution)

        handle = await kit.agent().start("Complete one bounded task")
        run_id = handle.id
        del handle
        gc.collect()
        await asyncio.sleep(0.05)
        assert len(kit._background) == 1  # held by the kit, not only by the handle
        release.set()
        await asyncio.sleep(0.3)
        record = await execution.runs.get(run_id=run_id, identity=kit.identity)
        assert record is not None and record.status is RuntimeRunStatus.COMPLETED
        assert not kit._background

    asyncio.run(scenario())


def _scheduler(graph: Any, workers: WorkerRegistry, execution: Any) -> Any:
    from kaiwen_agent.control import RunControl
    from kaiwen_agent.tasks import TaskScheduler

    store = execution.runs.store
    return TaskScheduler(
        graph=graph, workers=workers, store=store, control=RunControl(checkpoints=store)
    )
