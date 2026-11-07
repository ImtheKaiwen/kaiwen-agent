from __future__ import annotations

import asyncio
from collections import Counter

from pydantic import BaseModel

from kaiwen_agent import AgentContext, tool
from kaiwen_agent.events import AgentEvent, EventScope
from kaiwen_agent.persistence import SQLiteIdempotencyStore, SQLitePersistence
from kaiwen_agent.tools.execution import ToolExecutor
from kaiwen_agent.types import ToolCall


class WriteInput(BaseModel):
    value: str


def test_concurrent_owner_scopes_isolate_events_actions_and_receipts(tmp_path) -> None:
    database = tmp_path / "multitenant-load.sqlite3"
    owners = tuple((f"tenant-{index % 4}", f"user-{index}") for index in range(24))
    executions: Counter[tuple[str, str]] = Counter()

    @tool(
        name="records.write",
        description="Write one owner-scoped record",
        input_model=WriteInput,
        permissions={"records.write"},
        side_effect="reversible",
    )
    async def write_record(context: AgentContext, arguments: WriteInput) -> dict[str, str]:
        assert context.tenant_id is not None
        assert context.user_id is not None
        owner = (context.tenant_id, context.user_id)
        executions[owner] += 1
        return {
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "value": arguments.value,
            "action_intent_id": str(context.metadata["action_intent_id"]),
        }

    def context(tenant_id: str, user_id: str) -> AgentContext:
        return AgentContext(
            session_id="shared-session-id",
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id="shared-conversation-id",
            permissions=frozenset({"records.write"}),
        )

    call = ToolCall(
        id="shared-call-id",
        name="records.write",
        arguments={"value": "owner data"},
        idempotency_key="shared-idempotency-key",
    )

    async def scenario() -> None:
        store = SQLitePersistence(database)
        executor = ToolExecutor(
            action_intent_store=store,
            idempotency_store=SQLiteIdempotencyStore(database),
        )
        results = await asyncio.gather(
            *(
                executor.execute(
                    write_record,
                    call,
                    context(tenant_id, user_id),
                    execution_scope="shared-runtime-id",
                )
                for tenant_id, user_id in owners
            )
        )
        assert len({result.output["action_intent_id"] for result in results}) == len(owners)
        assert executions == Counter({owner: 1 for owner in owners})

        for index, (tenant_id, user_id) in enumerate(owners):
            await store.append(
                AgentEvent(
                    id=f"event-{index}",
                    type="tool.completed",
                    trace_id=f"trace-{index}",
                    session_id="shared-session-id",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    payload={"owner": f"{tenant_id}/{user_id}"},
                )
            )

        restarted_store = SQLitePersistence(database)
        restarted = ToolExecutor(
            action_intent_store=restarted_store,
            idempotency_store=SQLiteIdempotencyStore(database),
        )
        replayed = await asyncio.gather(
            *(
                restarted.execute(
                    write_record,
                    call,
                    context(tenant_id, user_id),
                    execution_scope="shared-runtime-id",
                )
                for tenant_id, user_id in owners
            )
        )
        assert executions == Counter({owner: 1 for owner in owners})
        assert [result.output for result in replayed] == [result.output for result in results]

        for index, (tenant_id, user_id) in enumerate(owners):
            visible = await restarted_store.list_events_scoped(
                scope=EventScope(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id="shared-session-id",
                )
            )
            assert [event.id for event in visible] == [f"event-{index}"]

        first_intent_id = results[0].output["action_intent_id"]
        assert (
            await restarted_store.get_action_intent(
                intent_id=first_intent_id,
                tenant_id=owners[1][0],
                user_id=owners[1][1],
            )
            is None
        )

    asyncio.run(scenario())
