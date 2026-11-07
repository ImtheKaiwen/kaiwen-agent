"""Idempotency and side-effect safety regressions (M5).

Reproduced before the fix: two concurrent identical side-effecting calls ran the handler
twice, and reusing an idempotency key with different arguments returned the first call's
cached result without executing or reporting a conflict.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from kaiwen_agent import AgentContext
from kaiwen_agent.exceptions import (
    ActionOutcomeUncertainError,
    DurableSideEffectsRequiredError,
    IdempotencyConflictError,
    ToolExecutionError,
)
from kaiwen_agent.models.fake import FakeModelProvider
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.runtime import AgentRuntime, AutonomyLevel, RuntimeIdentity
from kaiwen_agent.tools import ToolExecutor
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.tools.durability import ActionIntentStatus
from kaiwen_agent.types import ModelResponse, ToolCall


class _Args(BaseModel):
    amount: int = 0


class _Counter:
    def __init__(self, *, delay: float = 0.05, fail: bool = False) -> None:
        self.calls = 0
        self.seen: list[int] = []
        self.delay = delay
        self.fail = fail

    async def __call__(self, context: AgentContext, arguments: BaseModel) -> dict[str, int]:
        assert isinstance(arguments, _Args)
        self.calls += 1
        self.seen.append(arguments.amount)
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("external system rejected the request")
        return {"calls": self.calls, "amount": arguments.amount}


def _tool(handler: _Counter, *, side_effect: str = "reversible") -> ToolDefinition:
    return ToolDefinition(
        name="pay",
        description="Move money",
        input_model=_Args,
        handler=handler,
        permissions=frozenset(),
        side_effect=side_effect,  # type: ignore[arg-type]
    )


def _context() -> AgentContext:
    return AgentContext(session_id="session-1", tenant_id="tenant-1", user_id="user-1")


def _call(call_id: str = "call-1", *, amount: int = 1, key: str | None = None) -> ToolCall:
    return ToolCall(id=call_id, name="pay", arguments={"amount": amount}, idempotency_key=key)


def test_concurrent_identical_calls_execute_the_handler_once() -> None:
    async def scenario() -> None:
        handler = _Counter()
        executor = ToolExecutor()
        results = await asyncio.gather(
            executor.execute(_tool(handler), _call(), _context(), execution_scope="run-1"),
            executor.execute(_tool(handler), _call(), _context(), execution_scope="run-1"),
        )
        assert handler.calls == 1
        assert results[0].output == results[1].output

    asyncio.run(scenario())


def test_concurrent_calls_with_distinct_keys_still_run_independently() -> None:
    async def scenario() -> None:
        handler = _Counter()
        executor = ToolExecutor()
        await asyncio.gather(
            executor.execute(_tool(handler), _call("a"), _context(), execution_scope="run-1"),
            executor.execute(_tool(handler), _call("b"), _context(), execution_scope="run-1"),
        )
        assert handler.calls == 2

    asyncio.run(scenario())


def test_read_only_calls_without_a_key_are_not_deduplicated() -> None:
    async def scenario() -> None:
        handler = _Counter()
        executor = ToolExecutor()
        await asyncio.gather(
            executor.execute(
                _tool(handler, side_effect="none"), _call(), _context(), execution_scope="r"
            ),
            executor.execute(
                _tool(handler, side_effect="none"), _call(), _context(), execution_scope="r"
            ),
        )
        assert handler.calls == 2

    asyncio.run(scenario())


def test_key_reuse_with_different_arguments_is_rejected_after_completion() -> None:
    async def scenario() -> None:
        handler = _Counter(delay=0)
        executor = ToolExecutor()
        first = await executor.execute(
            _tool(handler), _call("c1", amount=1, key="k"), _context(), execution_scope="r"
        )
        with pytest.raises(IdempotencyConflictError):
            await executor.execute(
                _tool(handler), _call("c2", amount=999, key="k"), _context(), execution_scope="r"
            )
        assert handler.seen == [1]
        again = await executor.execute(
            _tool(handler), _call("c3", amount=1, key="k"), _context(), execution_scope="r"
        )
        assert again.output == first.output
        assert handler.calls == 1

    asyncio.run(scenario())


def test_key_reuse_with_different_arguments_is_rejected_while_in_flight() -> None:
    async def scenario() -> None:
        handler = _Counter()
        executor = ToolExecutor()
        first = asyncio.create_task(
            executor.execute(
                _tool(handler), _call("c1", amount=1, key="k"), _context(), execution_scope="r"
            )
        )
        await asyncio.sleep(0.01)
        with pytest.raises(IdempotencyConflictError):
            await executor.execute(
                _tool(handler), _call("c2", amount=2, key="k"), _context(), execution_scope="r"
            )
        await first
        assert handler.seen == [1]

    asyncio.run(scenario())


def test_failed_call_without_durable_store_is_not_cached_and_can_be_retried() -> None:
    async def scenario() -> None:
        handler = _Counter(delay=0, fail=True)
        executor = ToolExecutor()
        with pytest.raises(ToolExecutionError):
            await executor.execute(_tool(handler), _call(key="k"), _context(), execution_scope="r")
        handler.fail = False
        result = await executor.execute(
            _tool(handler), _call("c2", key="k"), _context(), execution_scope="r"
        )
        assert result.output == {"calls": 2, "amount": 1}

    asyncio.run(scenario())


def test_durable_concurrent_duplicates_execute_once(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "actions.sqlite3")
        handler = _Counter()
        executor = ToolExecutor(action_intent_store=store)
        results = await asyncio.gather(
            executor.execute(_tool(handler), _call(), _context(), execution_scope="run-1"),
            executor.execute(_tool(handler), _call(), _context(), execution_scope="run-1"),
        )
        assert handler.calls == 1
        assert results[0].output == results[1].output
        intents = await store.list_action_intents(statuses=frozenset(ActionIntentStatus), limit=10)
        assert [intent.status for intent in intents] == [ActionIntentStatus.SUCCEEDED]

    asyncio.run(scenario())


def test_durable_key_reuse_with_different_arguments_is_rejected(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLitePersistence(tmp_path / "actions.sqlite3")
        handler = _Counter(delay=0)
        executor = ToolExecutor(action_intent_store=store)
        await executor.execute(
            _tool(handler), _call("c1", amount=1, key="k"), _context(), execution_scope="r"
        )
        with pytest.raises(IdempotencyConflictError):
            await executor.execute(
                _tool(handler), _call("c2", amount=999, key="k"), _context(), execution_scope="r"
            )
        assert handler.seen == [1]

    asyncio.run(scenario())


def test_durable_receipt_and_uncertain_outcome_survive_restart(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "actions.sqlite3"
        handler = _Counter(delay=0)
        first = ToolExecutor(action_intent_store=SQLitePersistence(path))
        done = await first.execute(_tool(handler), _call(key="ok"), _context(), execution_scope="r")

        restarted = ToolExecutor(action_intent_store=SQLitePersistence(path))
        replay = await restarted.execute(
            _tool(handler), _call("again", key="ok"), _context(), execution_scope="r"
        )
        assert replay.output == done.output
        assert handler.calls == 1

        failing = _Counter(delay=0, fail=True)
        with pytest.raises(ActionOutcomeUncertainError):
            await restarted.execute(
                _tool(failing), _call("f1", key="bad"), _context(), execution_scope="r"
            )
        after_restart = ToolExecutor(action_intent_store=SQLitePersistence(path))
        with pytest.raises(ActionOutcomeUncertainError):
            await after_restart.execute(
                _tool(failing), _call("f2", key="bad"), _context(), execution_scope="r"
            )
        assert failing.calls == 1, "an uncertain action must never be replayed"

    asyncio.run(scenario())


def _runtime(executor: ToolExecutor, *, side_effect: str) -> AgentRuntime:
    return AgentRuntime(
        name="runtime",
        model=FakeModelProvider([ModelResponse(text="done")]),
        tools=[_tool(_Counter(delay=0), side_effect=side_effect)],
        tool_executor=executor,
    )


def _identity() -> RuntimeIdentity:
    return RuntimeIdentity(session_id="s", tenant_id="tenant-1", user_id="user-1")


def test_autonomous_side_effects_require_a_durable_action_store(tmp_path) -> None:
    async def scenario() -> None:
        runtime = _runtime(ToolExecutor(), side_effect="destructive")
        with pytest.raises(DurableSideEffectsRequiredError):
            await runtime.action(
                "go", identity=_identity(), tool_names=("pay",), autonomy=AutonomyLevel.AUTONOMOUS
            )

        durable = _runtime(
            ToolExecutor(action_intent_store=SQLitePersistence(tmp_path / "a.sqlite3")),
            side_effect="destructive",
        )
        result = await durable.action(
            "go", identity=_identity(), tool_names=("pay",), autonomy=AutonomyLevel.AUTONOMOUS
        )
        assert result.text == "done"

    asyncio.run(scenario())


def test_local_supervised_and_read_only_usage_needs_no_durable_store() -> None:
    async def scenario() -> None:
        supervised = _runtime(ToolExecutor(), side_effect="destructive")
        result = await supervised.action(
            "go",
            identity=_identity(),
            tool_names=("pay",),
            autonomy=AutonomyLevel.SUPERVISED,
        )
        assert result.text == "done"

        read_only = _runtime(ToolExecutor(), side_effect="none")
        result = await read_only.action(
            "go",
            identity=_identity(),
            tool_names=("pay",),
            autonomy=AutonomyLevel.AUTONOMOUS,
        )
        assert result.text == "done"

    asyncio.run(scenario())


def test_cancelled_first_call_resolves_waiting_duplicates_without_hanging() -> None:
    async def scenario() -> None:
        handler = _Counter(delay=30)
        executor = ToolExecutor()
        first = asyncio.create_task(
            executor.execute(_tool(handler), _call(), _context(), execution_scope="r")
        )
        await asyncio.sleep(0.02)
        second = asyncio.create_task(
            executor.execute(_tool(handler), _call(), _context(), execution_scope="r")
        )
        await asyncio.sleep(0.02)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(ToolExecutionError):
            await asyncio.wait_for(second, timeout=5)
        assert handler.calls == 1

    asyncio.run(scenario())
