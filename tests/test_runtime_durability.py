from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace

import pytest
from pydantic import BaseModel

from kaiwen_agent import AgentContext, tool
from kaiwen_agent.coordination import InMemoryLeaseStore
from kaiwen_agent.exceptions import (
    ActionOutcomeUncertainError,
    ApprovalDeniedError,
    DurableApprovalRequiredError,
)
from kaiwen_agent.persistence import SQLiteIdempotencyStore, SQLitePersistence
from kaiwen_agent.runtime import (
    ActionIntentRecord,
    ActionIntentStatus,
    AutonomyLevel,
    DurableRunManager,
    DurableRuntimeStores,
    RuntimeIdentity,
    RuntimeRecoveryManager,
    RuntimeRunStatus,
    RuntimeVersionPins,
)
from kaiwen_agent.tools.approval import ApprovalRequest, DurableApprovalGate
from kaiwen_agent.tools.execution import ToolExecutor
from kaiwen_agent.types import ToolCall, ToolResult


class WriteInput(BaseModel):
    value: str


def _pins() -> RuntimeVersionPins:
    return RuntimeVersionPins(
        workspace_id="workspace",
        workspace_version=1,
        agent_id="agent",
        agent_version=2,
        prompt_id="prompt",
        prompt_version=3,
        prompt_checksum="prompt-checksum",
        capability_manifest_id="agent",
        capability_manifest_version=4,
        provider_ref="fake",
        tool_names=("records.write",),
        toolset_checksum="toolset-checksum",
    )


def _identity(*, user_id: str = "user-1") -> RuntimeIdentity:
    return RuntimeIdentity(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id=user_id,
        conversation_id="conversation-1",
    )


def test_runtime_run_persists_owner_scope_pins_and_safe_transitions(tmp_path) -> None:
    database = tmp_path / "runtime.sqlite3"

    async def scenario() -> None:
        manager = DurableRunManager(SQLitePersistence(database))
        created = await manager.create(
            identity=_identity(),
            autonomy=AutonomyLevel.SUPERVISED,
            version_pins=_pins(),
        )
        running = await manager.start(run_id=created.id, identity=_identity())
        paused = await manager.pause(
            run_id=created.id,
            identity=_identity(),
            safe_boundary="task:one",
        )
        assert running.status == RuntimeRunStatus.RUNNING
        assert paused.safe_boundary == "task:one"

        restarted = DurableRunManager(SQLitePersistence(database))
        recovered = await restarted.get(run_id=created.id, identity=_identity())
        assert recovered is not None and recovered.version_pins == _pins()
        assert await restarted.get(run_id=created.id, identity=_identity(user_id="other")) is None

        with pytest.raises(ValueError, match="cannot transition"):
            await restarted.complete(run_id=created.id, identity=_identity())

    asyncio.run(scenario())


def test_durable_approval_and_action_receipt_survive_restart(tmp_path) -> None:
    database = tmp_path / "actions.sqlite3"
    executions = 0

    @tool(
        name="records.write",
        description="Write a record",
        input_model=WriteInput,
        permissions={"records.write"},
        side_effect="reversible",
        requires_approval=True,
    )
    async def write_record(context: AgentContext, arguments: WriteInput) -> str:
        nonlocal executions
        assert context.metadata["action_intent_id"].startswith("intent_")
        executions += 1
        return arguments.value

    async def scenario() -> None:
        context = AgentContext(
            session_id="session-1",
            tenant_id="tenant-1",
            user_id="user-1",
            conversation_id="conversation-1",
            permissions=frozenset({"records.write"}),
        )
        call = ToolCall(
            id="call-1",
            name="records.write",
            arguments={"value": "saved"},
            idempotency_key="record-1",
        )
        first_store = SQLitePersistence(database)
        gate = DurableApprovalGate(first_store)
        executor = ToolExecutor(
            approval_gate=gate,
            action_intent_store=first_store,
            idempotency_store=SQLiteIdempotencyStore(database),
        )
        with pytest.raises(DurableApprovalRequiredError) as pending:
            await executor.execute(write_record, call, context, execution_scope="runtime-1")
        approval = await first_store.get_approval(
            approval_id=pending.value.approval_id,
            tenant_id="tenant-1",
            user_id="user-1",
        )
        assert approval is not None

        with pytest.raises(KeyError):
            await gate.resolve(
                approval_id=pending.value.approval_id,
                tenant_id="tenant-1",
                user_id="other-user",
                conversation_id=approval.conversation_id,
                action_intent_id=approval.action_intent_id,
                tool_name=approval.tool_name,
                call_id=approval.call_id,
                decided_by="admin-1",
                approved=True,
            )
        await gate.resolve(
            approval_id=pending.value.approval_id,
            tenant_id="tenant-1",
            user_id="user-1",
            conversation_id=approval.conversation_id,
            action_intent_id=approval.action_intent_id,
            tool_name=approval.tool_name,
            call_id=approval.call_id,
            decided_by="admin-1",
            approved=True,
        )
        first = await executor.execute(write_record, call, context, execution_scope="runtime-1")
        assert first.output == "saved"

        restarted_store = SQLitePersistence(database)
        restarted = ToolExecutor(
            approval_gate=DurableApprovalGate(restarted_store),
            action_intent_store=restarted_store,
            idempotency_store=SQLiteIdempotencyStore(database),
        )
        replay = await restarted.execute(write_record, call, context, execution_scope="runtime-1")
        assert replay.output == "saved"

        with pytest.raises(KeyError):
            await gate.resolve(
                approval_id=pending.value.approval_id,
                tenant_id="tenant-1",
                user_id="user-1",
                conversation_id=approval.conversation_id,
                action_intent_id=approval.action_intent_id,
                tool_name=approval.tool_name,
                call_id=approval.call_id,
                decided_by="admin-1",
                approved=True,
            )

    asyncio.run(scenario())
    assert executions == 1


def test_consumed_approval_can_resume_only_while_action_is_still_prepared(tmp_path) -> None:
    database = tmp_path / "approval-crash-window.sqlite3"
    executions = 0

    @tool(
        name="records.write",
        description="Write a record",
        input_model=WriteInput,
        permissions={"records.write"},
        side_effect="reversible",
        requires_approval=True,
    )
    async def write_record(context: AgentContext, arguments: WriteInput) -> str:
        nonlocal executions
        del context
        executions += 1
        return arguments.value

    async def scenario() -> None:
        store = SQLitePersistence(database)
        gate = DurableApprovalGate(store)
        executor = ToolExecutor(approval_gate=gate, action_intent_store=store)
        context = AgentContext(
            session_id="session-1",
            tenant_id="tenant-1",
            user_id="user-1",
            conversation_id="conversation-1",
            permissions=frozenset({"records.write"}),
        )
        call = ToolCall(id="call-1", name="records.write", arguments={"value": "saved"})
        with pytest.raises(DurableApprovalRequiredError) as pending:
            await executor.execute(write_record, call, context, execution_scope="runtime-1")
        approval = await store.get_approval(
            approval_id=pending.value.approval_id,
            tenant_id="tenant-1",
            user_id="user-1",
        )
        assert approval is not None
        with pytest.raises(KeyError):
            await gate.resolve(
                approval_id=pending.value.approval_id,
                tenant_id="tenant-1",
                user_id="user-1",
                conversation_id="conversation-2",
                action_intent_id=approval.action_intent_id,
                tool_name=approval.tool_name,
                call_id=approval.call_id,
                decided_by="admin-1",
                approved=True,
            )
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
        assert await gate.approve(
            ApprovalRequest(
                tool_name=approval.tool_name,
                call_id=approval.call_id,
                arguments=call.arguments,
                side_effect="reversible",
                execution_scope=approval.runtime_run_id,
                action_intent_id=approval.action_intent_id,
                arguments_checksum=approval.arguments_checksum,
            ),
            context,
        )

        restarted_store = SQLitePersistence(database)
        resumed = await ToolExecutor(
            approval_gate=DurableApprovalGate(restarted_store),
            action_intent_store=restarted_store,
        ).execute(write_record, call, context, execution_scope="runtime-1")
        assert resumed.output == "saved"

    asyncio.run(scenario())
    assert executions == 1


def test_approval_cannot_cross_conversation_or_changed_arguments(tmp_path) -> None:
    database = tmp_path / "approval-binding.sqlite3"

    @tool(
        name="records.write",
        description="Write a record",
        input_model=WriteInput,
        permissions={"records.write"},
        side_effect="reversible",
        requires_approval=True,
    )
    async def write_record(context: AgentContext, arguments: WriteInput) -> str:
        del context
        return arguments.value

    async def scenario() -> None:
        store = SQLitePersistence(database)
        gate = DurableApprovalGate(store)
        executor = ToolExecutor(approval_gate=gate, action_intent_store=store)
        base_context = AgentContext(
            session_id="session-1",
            tenant_id="tenant-1",
            user_id="user-1",
            conversation_id="conversation-1",
            permissions=frozenset({"records.write"}),
        )
        first_call = ToolCall(
            id="call-1",
            name="records.write",
            arguments={"value": "first"},
            idempotency_key="stable-key",
        )
        with pytest.raises(DurableApprovalRequiredError) as first_pending:
            await executor.execute(
                write_record, first_call, base_context, execution_scope="runtime-1"
            )
        approval = await store.get_approval(
            approval_id=first_pending.value.approval_id,
            tenant_id="tenant-1",
            user_id="user-1",
        )
        assert approval is not None
        await gate.resolve(
            approval_id=first_pending.value.approval_id,
            tenant_id="tenant-1",
            user_id="user-1",
            conversation_id=approval.conversation_id,
            action_intent_id=approval.action_intent_id,
            tool_name=approval.tool_name,
            call_id=approval.call_id,
            decided_by="admin-1",
            approved=True,
        )

        other_conversation = replace(base_context, conversation_id="conversation-2")
        with pytest.raises(ValueError, match="different binding"):
            await executor.execute(
                write_record,
                first_call,
                other_conversation,
                execution_scope="runtime-1",
            )
        changed = first_call.model_copy(update={"arguments": {"value": "changed"}})
        with pytest.raises(ValueError, match="different binding"):
            await executor.execute(write_record, changed, base_context, execution_scope="runtime-1")

    asyncio.run(scenario())


def test_recovery_marks_inflight_action_uncertain_and_blocks_run(tmp_path) -> None:
    database = tmp_path / "recovery.sqlite3"

    async def scenario() -> None:
        store = SQLitePersistence(database)
        manager = DurableRunManager(store)
        run = await manager.create(
            identity=_identity(),
            autonomy=AutonomyLevel.AUTONOMOUS,
            version_pins=_pins(),
        )
        await manager.start(run_id=run.id, identity=_identity())
        intent = ActionIntentRecord(
            id="intent-inflight",
            runtime_run_id=run.id,
            tenant_id="tenant-1",
            user_id="user-1",
            conversation_id="conversation-1",
            tool_name="records.write",
            call_id="call-1",
            arguments_checksum="a" * 64,
            idempotency_key="record-1",
            side_effect="reversible",
        )
        await store.prepare_action_intent(intent)
        await store.transition_action_intent(
            intent_id=intent.id,
            tenant_id=intent.tenant_id,
            user_id=intent.user_id,
            expected=frozenset({ActionIntentStatus.PREPARED}),
            status=ActionIntentStatus.EXECUTING,
        )

        restarted = SQLitePersistence(database)
        recovery = RuntimeRecoveryManager(
            stores=DurableRuntimeStores(
                runs=restarted,
                actions=restarted,
                approvals=restarted,
            ),
            leases=InMemoryLeaseStore(),
        )
        report = await recovery.reconcile(owner_id="process-2")
        assert report.uncertain_action_ids == (intent.id,)
        recovered_intent = await restarted.get_action_intent(
            intent_id=intent.id,
            tenant_id=intent.tenant_id,
            user_id=intent.user_id,
        )
        recovered_run = await restarted.get_runtime_run(
            run_id=run.id,
            tenant_id=run.tenant_id,
            user_id=run.user_id,
        )
        assert recovered_intent is not None
        assert recovered_intent.status == ActionIntentStatus.UNCERTAIN
        assert recovered_run is not None and recovered_run.status == RuntimeRunStatus.BLOCKED

    asyncio.run(scenario())


def test_durable_idempotency_receipt_survives_restart(tmp_path) -> None:
    database = tmp_path / "receipts.sqlite3"

    async def scenario() -> None:
        first = SQLiteIdempotencyStore(database)
        await first.put("owner:tool:key", ToolResult(call_id="call-1", name="tool", output=7))
        restarted = SQLiteIdempotencyStore(database)
        receipt = await restarted.get("owner:tool:key")
        assert receipt is not None and receipt.output == 7

    asyncio.run(scenario())


def test_durable_configuration_rejects_memory_only_stores() -> None:
    class MemoryStore:
        is_durable = False

    store = MemoryStore()
    with pytest.raises(ValueError, match="requires durable"):
        DurableRuntimeStores(  # type: ignore[arg-type]
            runs=store,
            actions=store,
            approvals=store,
        ).validate()


def test_failed_side_effect_becomes_uncertain_instead_of_blind_retry(tmp_path) -> None:
    database = tmp_path / "uncertain.sqlite3"

    @tool(
        name="records.write",
        description="Write then lose acknowledgement",
        input_model=WriteInput,
        permissions={"records.write"},
        side_effect="destructive",
    )
    async def fail_after_write(context: AgentContext, arguments: WriteInput) -> str:
        del context, arguments
        raise ConnectionError("acknowledgement lost")

    async def scenario() -> None:
        store = SQLitePersistence(database)
        executor = ToolExecutor(action_intent_store=store)
        context = AgentContext(
            session_id="session-1",
            tenant_id="tenant-1",
            user_id="user-1",
            conversation_id="conversation-1",
            permissions=frozenset({"records.write"}),
        )
        call = ToolCall(id="call-1", name="records.write", arguments={"value": "x"})
        with pytest.raises(ActionOutcomeUncertainError) as failed:
            await executor.execute(fail_after_write, call, context, execution_scope="runtime-1")
        with pytest.raises(ActionOutcomeUncertainError):
            await executor.execute(fail_after_write, call, context, execution_scope="runtime-1")
        intent = await store.get_action_intent(
            intent_id=failed.value.action_intent_id,
            tenant_id="tenant-1",
            user_id="user-1",
        )
        assert intent is not None and intent.status == ActionIntentStatus.UNCERTAIN

    asyncio.run(scenario())


def test_denied_approval_cancels_prepared_action(tmp_path) -> None:
    database = tmp_path / "approval-denied.sqlite3"

    @tool(
        name="records.write",
        description="Write a record",
        input_model=WriteInput,
        permissions={"records.write"},
        side_effect="reversible",
        requires_approval=True,
    )
    async def write_record(context: AgentContext, arguments: WriteInput) -> str:
        del context
        return arguments.value

    async def scenario() -> None:
        store = SQLitePersistence(database)
        gate = DurableApprovalGate(store)
        executor = ToolExecutor(approval_gate=gate, action_intent_store=store)
        context = AgentContext(
            session_id="session-1",
            tenant_id="tenant-1",
            user_id="user-1",
            conversation_id="conversation-1",
            permissions=frozenset({"records.write"}),
        )
        call = ToolCall(id="call-1", name="records.write", arguments={"value": "x"})
        with pytest.raises(DurableApprovalRequiredError) as pending:
            await executor.execute(write_record, call, context, execution_scope="runtime-1")
        approval = await store.get_approval(
            approval_id=pending.value.approval_id,
            tenant_id="tenant-1",
            user_id="user-1",
        )
        assert approval is not None
        await gate.resolve(
            approval_id=approval.id,
            tenant_id=approval.tenant_id,
            user_id=approval.user_id,
            conversation_id=approval.conversation_id,
            action_intent_id=approval.action_intent_id,
            tool_name=approval.tool_name,
            call_id=approval.call_id,
            decided_by="admin-1",
            approved=False,
        )
        with pytest.raises(ApprovalDeniedError):
            await executor.execute(write_record, call, context, execution_scope="runtime-1")
        intent = await store.get_action_intent(
            intent_id=approval.action_intent_id,
            tenant_id=approval.tenant_id,
            user_id=approval.user_id,
        )
        assert intent is not None and intent.status == ActionIntentStatus.CANCELLED

    asyncio.run(scenario())


def test_sqlite_runtime_schema_is_added_to_an_existing_database(tmp_path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE sessions (
                id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """INSERT INTO sessions VALUES
            ('legacy-session', '{}', '2026-01-01T00:00:00+00:00',
             '2026-01-01T00:00:00+00:00')"""
        )

    async def scenario() -> None:
        store = SQLitePersistence(database)
        await store.initialize()
        session = await store.get_session("legacy-session")
        assert session is not None

    asyncio.run(scenario())
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                WHERE type = 'table' AND name IN
                    ('runtime_runs', 'action_intents', 'durable_approvals')"""
            )
        }
    assert tables == {"runtime_runs", "action_intents", "durable_approvals"}
