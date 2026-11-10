"""What the durable action pipeline does and does not deduplicate (PR #14 audit R2, finding 3).

Same identity is deduplicated from the durable receipt. An equivalent side effect issued under a
new identity is a different action and runs again: the platform cannot know that two calls mean the
same external operation unless the application supplies a stable idempotency key. These tests pin
that distinction with the real Agent -> ToolExecutor -> SQLite path; they do not claim exactly-once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from kaiwen_agent.tools.durability import ActionIntentStatus
from kaiwen_agent.types import ModelResponse
from kaiwen_agent.workflow import WorkflowOutcomeStatus
from tests.test_agent_runtime_worker import plan, spec
from tests.test_durable_action_scope import (
    Effects,
    Script,
    add_worker,
    build,
    execute,
    intents,
    tool_call,
)

RETRY = {"retry_policy": {"max_attempts": 2, "retry_on": ["execution_error"]}}


async def retried(tmp_path: Path, first: ModelResponse, second: ModelResponse) -> tuple[int, int]:
    flow, _ = build(tmp_path)
    effects = Effects()
    model = Script(first, RuntimeError("model transport failed"), second, ModelResponse(text="ok"))
    add_worker(flow, "w", model, effects)
    run = await flow.new_run()
    outcome = await execute(flow, run, plan(spec("t", "w", **RETRY)))
    assert outcome.status == WorkflowOutcomeStatus.COMPLETED and outcome.tasks[0].attempts == 2
    stored = await intents(flow, run.id)
    assert all(item.status == ActionIntentStatus.SUCCEEDED for item in stored)
    return effects.count, len(stored)


def test_a_rerun_with_the_same_call_id_is_answered_from_the_receipt(tmp_path: Path) -> None:
    async def scenario() -> None:
        count, stored = await retried(tmp_path, tool_call("call-A"), tool_call("call-A"))
        assert (count, stored) == (1, 1)  # same task and call id: one effect, one intent

    asyncio.run(scenario())


def test_an_equivalent_call_under_a_new_call_id_and_no_key_runs_again(tmp_path: Path) -> None:
    async def scenario() -> None:
        count, stored = await retried(tmp_path, tool_call("call-A"), tool_call("call-B"))
        # Identical arguments, but no stable key binds the two calls: the platform cannot tell it
        # is the same external operation, so the effect happens twice. Not exactly-once.
        assert (count, stored) == (2, 2)

    asyncio.run(scenario())


def test_a_stable_application_key_binds_calls_with_different_call_ids(tmp_path: Path) -> None:
    async def scenario() -> None:
        count, stored = await retried(
            tmp_path, tool_call("call-A", key="record-7"), tool_call("call-B", key="record-7")
        )
        assert (count, stored) == (1, 1)  # the key, not the call id, is the identity

    asyncio.run(scenario())
