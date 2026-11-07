import asyncio

import pytest
from pydantic import BaseModel

from kaiwen_agent import AgentContext, tool
from kaiwen_agent.audit import InMemoryAuditStore
from kaiwen_agent.events import InMemoryEventSink
from kaiwen_agent.exceptions import (
    ApprovalRequiredError,
    PermissionDeniedError,
    ToolValidationError,
)
from kaiwen_agent.policy import (
    CapabilityPolicy,
    DelegationPolicy,
    PolicyContext,
    PolicyDecision,
    PolicyDecisionType,
    PolicyEngine,
    PolicyRegistry,
    RuntimePolicy,
    SchedulingPolicy,
)
from kaiwen_agent.tools.approval import StaticApprovalGate
from kaiwen_agent.tools.execution import ToolExecutor
from kaiwen_agent.types import ToolCall


class FixedPolicy:
    def __init__(self, policy_id: str, decision: PolicyDecisionType, reason: str = "test") -> None:
        self.policy_id = policy_id
        self.decision = decision
        self.reason = reason

    async def evaluate(self, context: PolicyContext) -> PolicyDecision:
        del context
        return PolicyDecision(
            decision=self.decision,
            policy_id=self.policy_id,
            rule_id="fixed",
            reason=self.reason,
        )


def policy_context(**metadata) -> PolicyContext:
    return PolicyContext(
        tenant_id="tenant_1",
        user_id="user_1",
        agent_id="admin-agent",
        session_id="session_1",
        trace_id="trace_1",
        action="runtime.action",
        capabilities=frozenset({"runtime.execute"}),
        metadata=metadata,
    )


def test_policy_engine_precedence_is_fail_closed_and_audited() -> None:
    async def scenario() -> None:
        audit = InMemoryAuditStore()
        events = InMemoryEventSink()
        engine = PolicyEngine(
            registry=PolicyRegistry(
                [
                    FixedPolicy("approval", PolicyDecisionType.REQUIRE_APPROVAL),
                    FixedPolicy("deny", PolicyDecisionType.DENY),
                ]
            ),
            audit_store=audit,
            events=events,
        )
        decision = await engine.evaluate(policy_context(actions_in_cycle=0))
        assert decision.decision == PolicyDecisionType.DENY
        records = await audit.list_audit(trace_id="trace_1")
        assert [record.details["decision"] for record in records] == [
            "require_approval",
            "deny",
        ]
        assert [event.type for event in events.events] == [
            "policy.approval_required",
            "policy.denied",
        ]

    asyncio.run(scenario())


def test_policy_engine_defaults_to_deny_without_matching_rule() -> None:
    decision = asyncio.run(PolicyEngine().evaluate(policy_context()))
    assert decision.decision == PolicyDecisionType.DENY
    assert decision.rule_id == "default"


def test_capability_runtime_scheduling_and_delegation_policies() -> None:
    async def scenario() -> None:
        capability = CapabilityPolicy({"runtime.action": {"runtime.execute"}})
        allowed = await capability.evaluate(policy_context(actions_in_cycle=0))
        assert allowed is not None and allowed.decision == PolicyDecisionType.ALLOW

        runtime = RuntimePolicy(max_actions_per_cycle=2)
        denied = await runtime.evaluate(policy_context(actions_in_cycle=2))
        assert denied is not None and denied.decision == PolicyDecisionType.DENY

        scheduling = SchedulingPolicy(
            min_interval_seconds=60,
            max_interval_seconds=3600,
            max_wakeups_per_hour=4,
        )
        schedule_context = policy_context(interval_seconds=10.0, wakeups_last_hour=0)
        schedule_context.action = "runtime.schedule_wakeup"
        modified = await scheduling.evaluate(schedule_context)
        assert modified is not None
        assert modified.decision == PolicyDecisionType.MODIFY
        assert modified.modifications == {"interval_seconds": 60}

        delegation = DelegationPolicy(
            max_depth=2,
            max_parallel_jobs=3,
            allowed_delegates={"research-agent"},
        )
        delegation_context = policy_context(delegation_depth=1, parallel_jobs=0)
        delegation_context.action = "agent.delegate"
        delegation_context.resource = "research-agent"
        delegated = await delegation.evaluate(delegation_context)
        assert delegated is not None
        assert delegated.decision == PolicyDecisionType.ALLOW

    asyncio.run(scenario())


class WriteInput(BaseModel):
    value: str


def test_tool_policy_deny_prevents_side_effect() -> None:
    executions = 0

    @tool(
        name="records.write",
        description="Write a record",
        input_model=WriteInput,
        permissions={"records.write"},
        side_effect="reversible",
    )
    async def write_record(context: AgentContext, arguments: WriteInput) -> str:
        nonlocal executions
        del context
        executions += 1
        return arguments.value

    async def scenario() -> None:
        engine = PolicyEngine(
            registry=PolicyRegistry(
                [FixedPolicy("product-policy", PolicyDecisionType.DENY, "blocked")]
            )
        )
        with pytest.raises(PermissionDeniedError, match="blocked"):
            await ToolExecutor(policy_engine=engine).execute(
                write_record,
                ToolCall(
                    id="call_1",
                    name="records.write",
                    arguments={"value": "unsafe"},
                ),
                AgentContext(
                    session_id="session_1",
                    permissions=frozenset({"records.write"}),
                ),
                execution_scope="run_1",
            )

    asyncio.run(scenario())
    assert executions == 0


def test_custom_allow_cannot_bypass_missing_permission() -> None:
    @tool(
        name="records.protected",
        description="Protected operation",
        input_model=WriteInput,
        permissions={"records.write"},
    )
    async def protected(context: AgentContext, arguments: WriteInput) -> str:
        del context
        return arguments.value

    async def scenario() -> None:
        engine = PolicyEngine(
            registry=PolicyRegistry([FixedPolicy("product-allow", PolicyDecisionType.ALLOW)])
        )
        with pytest.raises(PermissionDeniedError):
            await ToolExecutor(policy_engine=engine).execute(
                protected,
                ToolCall(
                    id="call_1",
                    name="records.protected",
                    arguments={"value": "x"},
                ),
                AgentContext(session_id="session_1"),
                execution_scope="run_1",
            )

    asyncio.run(scenario())


def test_custom_allow_cannot_bypass_definition_approval() -> None:
    @tool(
        name="records.approved",
        description="Approval protected operation",
        input_model=WriteInput,
        requires_approval=True,
    )
    async def approved(context: AgentContext, arguments: WriteInput) -> str:
        del context
        return arguments.value

    async def scenario() -> None:
        engine = PolicyEngine(
            registry=PolicyRegistry([FixedPolicy("product-allow", PolicyDecisionType.ALLOW)])
        )
        with pytest.raises(ApprovalRequiredError):
            await ToolExecutor(policy_engine=engine).execute(
                approved,
                ToolCall(
                    id="call_1",
                    name="records.approved",
                    arguments={"value": "x"},
                ),
                AgentContext(session_id="session_1"),
                execution_scope="run_1",
            )
        result = await ToolExecutor(
            policy_engine=engine,
            approval_gate=StaticApprovalGate(True),
        ).execute(
            approved,
            ToolCall(
                id="call_2",
                name="records.approved",
                arguments={"value": "approved"},
            ),
            AgentContext(session_id="session_1"),
            execution_scope="run_2",
        )
        assert result.output == "approved"

    asyncio.run(scenario())


def test_policy_can_require_approval_for_safe_tool() -> None:
    @tool(
        name="records.read",
        description="Read a record",
        input_model=WriteInput,
    )
    async def read_record(context: AgentContext, arguments: WriteInput) -> str:
        del context
        return arguments.value

    async def scenario() -> None:
        engine = PolicyEngine(
            registry=PolicyRegistry([FixedPolicy("review", PolicyDecisionType.REQUIRE_APPROVAL)])
        )
        with pytest.raises(ApprovalRequiredError):
            await ToolExecutor(policy_engine=engine).execute(
                read_record,
                ToolCall(
                    id="call_1",
                    name="records.read",
                    arguments={"value": "x"},
                ),
                AgentContext(session_id="session_1"),
                execution_scope="run_1",
            )

    asyncio.run(scenario())


def test_policy_modification_is_revalidated_before_execution() -> None:
    class ModifyPolicy:
        policy_id = "normalize"

        async def evaluate(self, context: PolicyContext) -> PolicyDecision:
            del context
            return PolicyDecision(
                decision=PolicyDecisionType.MODIFY,
                policy_id=self.policy_id,
                rule_id="normalize-value",
                reason="Normalize the input",
                modifications={"arguments": {"value": "normalized"}},
            )

    @tool(
        name="records.normalize",
        description="Normalize a record",
        input_model=WriteInput,
    )
    async def normalize(context: AgentContext, arguments: WriteInput) -> str:
        del context
        return arguments.value

    async def scenario() -> None:
        result = await ToolExecutor(
            policy_engine=PolicyEngine(registry=PolicyRegistry([ModifyPolicy()]))
        ).execute(
            normalize,
            ToolCall(
                id="call_1",
                name="records.normalize",
                arguments={"value": "raw"},
            ),
            AgentContext(session_id="session_1"),
            execution_scope="run_1",
        )
        assert result.output == "normalized"

    asyncio.run(scenario())


def test_invalid_policy_modification_never_reaches_handler() -> None:
    executions = 0

    class InvalidModifyPolicy:
        policy_id = "invalid-modification"

        async def evaluate(self, context: PolicyContext) -> PolicyDecision:
            del context
            return PolicyDecision(
                decision=PolicyDecisionType.MODIFY,
                policy_id=self.policy_id,
                rule_id="invalid-value",
                reason="Produce an invalid value for the regression test",
                modifications={"arguments": {"value": 42}},
            )

    @tool(
        name="records.validated",
        description="Validated operation",
        input_model=WriteInput,
    )
    async def validated(context: AgentContext, arguments: WriteInput) -> str:
        nonlocal executions
        del context
        executions += 1
        return arguments.value

    async def scenario() -> None:
        with pytest.raises(ToolValidationError):
            await ToolExecutor(
                policy_engine=PolicyEngine(registry=PolicyRegistry([InvalidModifyPolicy()]))
            ).execute(
                validated,
                ToolCall(
                    id="call_1",
                    name="records.validated",
                    arguments={"value": "valid"},
                ),
                AgentContext(session_id="session_1"),
                execution_scope="run_1",
            )

    asyncio.run(scenario())
    assert executions == 0
