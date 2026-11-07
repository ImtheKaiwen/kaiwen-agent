"""Upgrade compatibility with action intents persisted by the pre-M5 executor.

`tests/fixtures/legacy_action_intents.json` was produced by the real pre-M5 `ToolExecutor`
(commit 4c5ff65): one SUCCEEDED, one UNCERTAIN, one EXECUTING (a cancelled in-flight
call), and one PREPARED intent with its pending durable approval. Pre-M5 intent ids
included the arguments checksum; M5 ids do not, so an id lookup alone cannot find them.
An external side effect must never be repeated because a legacy record was not matched.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from kaiwen_agent import AgentContext
from kaiwen_agent.exceptions import ActionOutcomeUncertainError, IdempotencyConflictError
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.tools import ToolExecutor
from kaiwen_agent.tools.approval import DurableApprovalGate
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.tools.durability import (
    ActionIntentRecord,
    ActionIntentStatus,
    DurableApprovalRecord,
)
from kaiwen_agent.types import ToolCall

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_action_intents.json"
SCOPE = "run-legacy"
LEGACY = {
    "legacy-ok": (1, ActionIntentStatus.SUCCEEDED),
    "legacy-uncertain": (2, ActionIntentStatus.UNCERTAIN),
    "legacy-exec": (3, ActionIntentStatus.EXECUTING),
    "legacy-prep": (4, ActionIntentStatus.PREPARED),
}


class _Pay(BaseModel):
    amount: int


class _Handler:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def __call__(self, context: AgentContext, arguments: BaseModel) -> dict[str, int]:
        assert isinstance(arguments, _Pay)
        self.calls.append(arguments.amount)
        return {"paid": arguments.amount}


def _tool(handler: _Handler, *, approval: bool = True) -> ToolDefinition:
    return ToolDefinition(
        name="pay",
        description="pay",
        input_model=_Pay,
        handler=handler,
        permissions=frozenset(),
        side_effect="reversible",
        requires_approval=approval,
        timeout_seconds=120,
    )


def _context() -> AgentContext:
    return AgentContext(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
    )


def _call(key: str, amount: int, *, call_id: str | None = None) -> ToolCall:
    return ToolCall(
        id=call_id or f"call-{key}",
        name="pay",
        arguments={"amount": amount},
        idempotency_key=key,
    )


async def _load_legacy(store: SQLitePersistence) -> dict[str, ActionIntentRecord]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    loaded: dict[str, ActionIntentRecord] = {}
    for raw in payload["intents"]:
        record = ActionIntentRecord.model_validate(raw)
        await store.prepare_action_intent(
            record.model_copy(update={"status": ActionIntentStatus.PREPARED})
        )
        if record.status != ActionIntentStatus.PREPARED:
            await store.transition_action_intent(
                intent_id=record.id,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                expected=frozenset({ActionIntentStatus.PREPARED}),
                status=ActionIntentStatus.EXECUTING,
            )
        if record.status in {ActionIntentStatus.SUCCEEDED, ActionIntentStatus.UNCERTAIN}:
            await store.transition_action_intent(
                intent_id=record.id,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                expected=frozenset({ActionIntentStatus.EXECUTING}),
                status=record.status,
                result=record.result,
                error_code=record.error_code,
            )
        loaded[record.idempotency_key] = record
    for raw in payload["approvals"]:
        await store.create_pending_approval(DurableApprovalRecord.model_validate(raw))
    return loaded


def _setup(tmp_path: Path, handler: _Handler) -> tuple[SQLitePersistence, ToolExecutor, Any]:
    store = SQLitePersistence(tmp_path / "upgraded.sqlite3")
    gate = DurableApprovalGate(store)
    return store, ToolExecutor(action_intent_store=store, approval_gate=gate), gate


def test_fixture_holds_real_pre_m5_records_in_every_state() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    states = {item["idempotency_key"]: item["status"] for item in payload["intents"]}
    assert states == {key: status.value for key, (_, status) in LEGACY.items()}
    assert len(payload["approvals"]) == 1


def test_legacy_receipt_is_returned_without_running_the_handler_or_asking_again(tmp_path) -> None:
    async def scenario() -> None:
        handler = _Handler()
        store, executor, _ = _setup(tmp_path, handler)
        await _load_legacy(store)

        result = await executor.execute(
            _tool(handler), _call("legacy-ok", 1), _context(), execution_scope=SCOPE
        )
        assert result.output == {"paid": 1}
        different_call_id = await executor.execute(
            _tool(handler),
            _call("legacy-ok", 1, call_id="call-after-upgrade"),
            _context(),
            execution_scope=SCOPE,
        )
        assert different_call_id.output == {"paid": 1}
        assert handler.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["legacy-uncertain", "legacy-exec"])
def test_legacy_uncertain_and_executing_actions_are_never_replayed(tmp_path, key: str) -> None:
    async def scenario() -> None:
        handler = _Handler()
        store, executor, _ = _setup(tmp_path, handler)
        await _load_legacy(store)
        amount, _ = LEGACY[key]

        for call_id in (None, "call-after-upgrade"):
            with pytest.raises(ActionOutcomeUncertainError):
                await executor.execute(
                    _tool(handler, approval=False),
                    _call(key, amount, call_id=call_id),
                    _context(),
                    execution_scope=SCOPE,
                )
        assert handler.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("key", list(LEGACY))
def test_changed_arguments_against_a_legacy_record_are_a_conflict(tmp_path, key: str) -> None:
    async def scenario() -> None:
        handler = _Handler()
        store, executor, _ = _setup(tmp_path, handler)
        await _load_legacy(store)
        amount, _ = LEGACY[key]

        with pytest.raises(IdempotencyConflictError):
            await executor.execute(
                _tool(handler, approval=False),
                _call(key, amount + 1000),
                _context(),
                execution_scope=SCOPE,
            )
        assert handler.calls == []

    asyncio.run(scenario())


def test_legacy_prepared_action_resumes_once_under_its_original_intent_and_approval(
    tmp_path,
) -> None:
    async def scenario() -> None:
        handler = _Handler()
        store, executor, gate = _setup(tmp_path, handler)
        legacy = await _load_legacy(store)
        prepared = legacy["legacy-prep"]
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        approval = DurableApprovalRecord.model_validate(payload["approvals"][0])
        assert approval.action_intent_id == prepared.id

        await gate.resolve(
            approval_id=approval.id,
            tenant_id="tenant-1",
            user_id="user-1",
            conversation_id=approval.conversation_id,
            action_intent_id=approval.action_intent_id,
            tool_name=approval.tool_name,
            call_id=approval.call_id,
            decided_by="admin-1",
            approved=True,
        )
        result = await executor.execute(
            _tool(handler), _call("legacy-prep", 4), _context(), execution_scope=SCOPE
        )

        assert result.output == {"paid": 4}
        assert handler.calls == [4]
        intents = await store.list_action_intents(statuses=frozenset(ActionIntentStatus), limit=100)
        assert len(intents) == len(LEGACY), "a duplicate intent was created for a legacy record"
        stored = await store.get_action_intent(
            intent_id=prepared.id, tenant_id="tenant-1", user_id="user-1"
        )
        assert stored is not None and stored.status == ActionIntentStatus.SUCCEEDED

        replay = await executor.execute(
            _tool(handler), _call("legacy-prep", 4), _context(), execution_scope=SCOPE
        )
        assert replay.output == {"paid": 4}
        assert handler.calls == [4], "the action was executed twice"

    asyncio.run(scenario())


class _WithoutKeyLookup:
    """A durable store that lacks the optional by-key lookup."""

    def __init__(self, inner: SQLitePersistence) -> None:
        self._inner = inner
        self.is_durable = True

    def __getattr__(self, name: str) -> Any:
        if name == "get_action_intent_by_key":
            raise AttributeError(name)
        return getattr(self._inner, name)


@pytest.mark.parametrize("key", list(LEGACY))
def test_without_the_optional_key_lookup_legacy_replays_still_never_run_the_handler(
    tmp_path, key: str
) -> None:
    from kaiwen_agent.exceptions import DurableApprovalRequiredError

    async def scenario() -> None:
        handler = _Handler()
        store = SQLitePersistence(tmp_path / "upgraded.sqlite3")
        await _load_legacy(store)
        executor = ToolExecutor(
            action_intent_store=_WithoutKeyLookup(store),  # type: ignore[arg-type]
            approval_gate=DurableApprovalGate(store),
        )
        amount, _ = LEGACY[key]

        for call_id in (None, "call-after-upgrade"):
            # Failing closed is acceptable; executing the side effect again is not.
            with contextlib.suppress(
                ActionOutcomeUncertainError,
                DurableApprovalRequiredError,
                IdempotencyConflictError,
            ):
                await executor.execute(
                    _tool(handler),
                    _call(key, amount, call_id=call_id),
                    _context(),
                    execution_scope=SCOPE,
                )
        assert handler.calls == [], "a legacy action was executed again"

    asyncio.run(scenario())
