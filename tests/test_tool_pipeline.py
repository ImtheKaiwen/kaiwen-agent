import asyncio

import pytest
from pydantic import BaseModel

from kaiwen_agent import AgentContext, tool
from kaiwen_agent.audit import InMemoryAuditStore
from kaiwen_agent.exceptions import (
    ApprovalDeniedError,
    ApprovalRequiredError,
    ToolTimeoutError,
)
from kaiwen_agent.tools.approval import StaticApprovalGate
from kaiwen_agent.tools.definition import RetryPolicy
from kaiwen_agent.tools.execution import ToolExecutor
from kaiwen_agent.types import ToolCall


class WriteInput(BaseModel):
    value: str


def test_approval_is_required_and_can_be_denied() -> None:
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

    call = ToolCall(id="call_1", name="records.write", arguments={"value": "x"})
    context = AgentContext(session_id="session_1", permissions=frozenset({"records.write"}))

    with pytest.raises(ApprovalRequiredError):
        asyncio.run(ToolExecutor().execute(write_record, call, context, execution_scope="run_1"))
    with pytest.raises(ApprovalDeniedError):
        asyncio.run(
            ToolExecutor(approval_gate=StaticApprovalGate(False)).execute(
                write_record, call, context, execution_scope="run_1"
            )
        )


def test_side_effecting_tool_is_idempotent_per_execution_key() -> None:
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
        executor = ToolExecutor(approval_gate=StaticApprovalGate(True))
        call = ToolCall(
            id="call_1",
            name="records.write",
            arguments={"value": "saved"},
            idempotency_key="record:test-key",
        )
        context = AgentContext(session_id="session_1", permissions=frozenset({"records.write"}))
        first = await executor.execute(write_record, call, context, execution_scope="run_1")
        second = await executor.execute(write_record, call, context, execution_scope="run_2")
        assert first.output == second.output == "saved"

    asyncio.run(scenario())
    assert executions == 1


def test_timeout_is_classified_and_retried() -> None:
    executions = 0

    @tool(
        name="slow.read",
        description="Read slowly",
        input_model=WriteInput,
        timeout_seconds=0.001,
        retry_policy=RetryPolicy(max_attempts=2),
    )
    async def slow_read(context: AgentContext, arguments: WriteInput) -> str:
        nonlocal executions
        del context
        executions += 1
        await asyncio.sleep(0.02)
        return arguments.value

    with pytest.raises(ToolTimeoutError):
        asyncio.run(
            ToolExecutor().execute(
                slow_read,
                ToolCall(id="call_1", name="slow.read", arguments={"value": "x"}),
                AgentContext(session_id="session_1"),
                execution_scope="run_1",
            )
        )
    assert executions == 2


def test_tool_execution_writes_redacted_audit_record() -> None:
    @tool(
        name="records.write",
        description="Write a record",
        input_model=WriteInput,
        permissions={"records.write"},
        side_effect="reversible",
    )
    async def write_record(context: AgentContext, arguments: WriteInput) -> str:
        del context
        return arguments.value

    async def scenario() -> None:
        audit = InMemoryAuditStore()
        executor = ToolExecutor(audit_store=audit)
        result = await executor.execute(
            write_record,
            ToolCall(
                id="call_1",
                name="records.write",
                arguments={"value": "sk-abcdefghijklmnopqrstuvwxyz"},
            ),
            AgentContext(
                session_id="session_1",
                trace_id="trace_1",
                permissions=frozenset({"records.write"}),
            ),
            execution_scope="run_1",
        )
        records = await audit.list_audit(trace_id="trace_1")
        assert result.output == "sk-abcdefghijklmnopqrstuvwxyz"
        assert records[0].outcome == "succeeded"
        assert "sk-" not in str(records[0].details)

    asyncio.run(scenario())
