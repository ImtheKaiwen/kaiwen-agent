"""Opt-in recoverable tool errors and their safety boundaries (M5).

Fail-fast stays the default. In `tool_errors="return"` mode only correctable errors
(invalid arguments, unknown tool, and failures of read-only tools) become sanitized tool
results. Permission, policy, approval, security, and uncertain-side-effect errors never do.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel

from kaiwen_agent import Agent, AgentContext
from kaiwen_agent.exceptions import (
    ApprovalRequiredError,
    PermissionDeniedError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolValidationError,
)
from kaiwen_agent.integrations import WorkspacePathError
from kaiwen_agent.models.fake import FakeModelProvider
from kaiwen_agent.runtime import AgentRuntime, RuntimeIdentity
from kaiwen_agent.tools.definition import ToolDefinition
from kaiwen_agent.types import AgentInput, ModelResponse, ToolCall, ToolResult

SECRET = "sk-secretvalue1234567890"


class _Amount(BaseModel):
    amount: int


class _Empty(BaseModel):
    pass


def _tool(
    name: str,
    handler: Any,
    *,
    model: type[BaseModel] = _Empty,
    side_effect: str = "none",
    permissions: frozenset[str] = frozenset(),
    requires_approval: bool = False,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        input_model=model,
        handler=handler,
        permissions=permissions,
        side_effect=side_effect,  # type: ignore[arg-type]
        requires_approval=requires_approval,
    )


async def _boom(context: AgentContext, arguments: BaseModel) -> None:
    raise RuntimeError(f"upstream said {SECRET}")


async def _path_denied(context: AgentContext, arguments: BaseModel) -> None:
    raise WorkspacePathError("Workspace path is excluded by the host read policy")


async def _ok(context: AgentContext, arguments: BaseModel) -> dict[str, bool]:
    return {"ok": True}


class _Scripted:
    """Calls the given tool calls in order, then answers; records what it was sent."""

    def __init__(self, calls: list[ToolCall]) -> None:
        self.calls = calls
        self.turn = 0
        self.received: list[list[ToolResult]] = []

    async def respond(self, *args: Any, tool_results: Any = (), **kwargs: Any) -> ModelResponse:
        self.received.append(list(tool_results))
        if self.turn < len(self.calls):
            call = self.calls[self.turn]
            self.turn += 1
            return ModelResponse(tool_calls=[call])
        return ModelResponse(text="recovered")


def _agent(model: Any, tools: list[ToolDefinition], **options: Any) -> Agent:
    return Agent(name="a", model=model, tools=tools, **options)


def _run(agent: Agent) -> Any:
    return asyncio.run(
        agent.run(
            AgentInput(text="go"), context=AgentContext(session_id="s", tenant_id="t", user_id="u")
        )
    )


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (ToolCall(id="1", name="amount", arguments={"amount": "x"}), ToolValidationError),
        (ToolCall(id="1", name="missing", arguments={}), ToolNotFoundError),
        (ToolCall(id="1", name="boom", arguments={}), ToolExecutionError),
    ],
)
def test_fail_fast_remains_the_default(call: ToolCall, expected: type[Exception]) -> None:
    agent = _agent(
        _Scripted([call]),
        [_tool("amount", _ok, model=_Amount), _tool("boom", _boom)],
    )
    with pytest.raises(expected):
        _run(agent)


def test_validation_error_is_returned_sanitized_and_the_model_can_correct() -> None:
    model = _Scripted(
        [
            ToolCall(id="1", name="amount", arguments={"amount": SECRET}),
            ToolCall(id="2", name="amount", arguments={"amount": 5}),
        ]
    )
    agent = _agent(model, [_tool("amount", _ok, model=_Amount)], tool_errors="return")

    result = _run(agent)

    assert result.text == "recovered"
    error = result.tool_results[0]
    assert error.is_error is True
    payload = json.dumps(error.output)
    assert SECRET not in payload
    assert error.output["error"]["type"] == "validation_error"
    assert error.output["error"]["details"][0]["loc"] == ["amount"]
    assert result.tool_results[1].is_error is False
    assert model.received[1][0].is_error is True


def test_unknown_tool_and_read_only_failure_are_returned_without_internal_detail() -> None:
    model = _Scripted(
        [
            ToolCall(id="1", name="missing", arguments={}),
            ToolCall(id="2", name="boom", arguments={}),
        ]
    )
    agent = _agent(model, [_tool("boom", _boom)], tool_errors="return")

    result = _run(agent)

    assert result.text == "recovered"
    assert [item.output["error"]["type"] for item in result.tool_results] == [
        "unknown_tool",
        "tool_error",
    ]
    assert SECRET not in json.dumps([item.output for item in result.tool_results])


def test_failures_of_side_effecting_tools_are_never_returned_to_the_model() -> None:
    agent = _agent(
        _Scripted([ToolCall(id="1", name="send", arguments={})]),
        [_tool("send", _boom, side_effect="reversible")],
        tool_errors="return",
    )
    with pytest.raises(ToolExecutionError):
        _run(agent)


def test_security_and_policy_errors_are_never_returned_to_the_model() -> None:
    needs_permission = _tool("guarded", _ok, permissions=frozenset({"admin"}))
    needs_approval = _tool("approve", _ok, requires_approval=True)
    for call, tools, expected in (
        (ToolCall(id="1", name="guarded", arguments={}), [needs_permission], PermissionDeniedError),
        (ToolCall(id="1", name="approve", arguments={}), [needs_approval], ApprovalRequiredError),
        (
            ToolCall(id="1", name="denied", arguments={}),
            [_tool("denied", _path_denied)],
            ToolExecutionError,
        ),
    ):
        agent = _agent(_Scripted([call]), tools, tool_errors="return")
        with pytest.raises(expected):
            _run(agent)


def test_self_correction_is_bounded() -> None:
    bad = [ToolCall(id=str(i), name="amount", arguments={"amount": "x"}) for i in range(5)]
    agent = _agent(
        _Scripted(bad),
        [_tool("amount", _ok, model=_Amount)],
        tool_errors="return",
        max_tool_errors=2,
    )
    with pytest.raises(ToolValidationError):
        _run(agent)


def test_runtime_forwards_the_opt_in_and_validates_options() -> None:
    runtime = AgentRuntime(
        name="r",
        model=_Scripted([ToolCall(id="1", name="amount", arguments={"amount": "x"})]),
        tools=[_tool("amount", _ok, model=_Amount)],
        tool_errors="return",
    )
    result = asyncio.run(
        runtime.action("go", identity=RuntimeIdentity(session_id="s"), tool_names=("amount",))
    )
    assert result.tool_results[0].is_error is True

    with pytest.raises(ValueError):
        Agent(name="a", model=FakeModelProvider([]), tool_errors="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Agent(name="a", model=FakeModelProvider([]), tool_errors="return", max_tool_errors=0)
