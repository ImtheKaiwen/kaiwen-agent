from __future__ import annotations

import inspect

from kaiwen_agent import Agent, AgentContext, ModelProvider, ToolExecutor, tool
from kaiwen_agent.jobs import JobManager
from kaiwen_agent.tasks import TaskScheduler
from kaiwen_agent.types import AgentInput, AgentResult, ModelResponse


def test_legacy_root_imports_remain_available() -> None:
    assert Agent is not None
    assert AgentContext is not None
    assert ModelProvider is not None
    assert ToolExecutor is not None
    assert callable(tool)


def test_agent_and_provider_call_signatures_remain_compatible() -> None:
    assert tuple(inspect.signature(Agent.__init__).parameters) == (
        "self",
        "name",
        "model",
        "tools",
        "permissions",
        "event_sink",
        "run_store",
        "tool_executor",
        "max_steps",
    )
    run_parameters = inspect.signature(Agent.run).parameters
    assert tuple(run_parameters) == ("self", "agent_input", "context")
    assert run_parameters["context"].kind == inspect.Parameter.KEYWORD_ONLY

    provider_parameters = inspect.signature(ModelProvider.respond).parameters
    assert tuple(provider_parameters) == (
        "self",
        "agent_input",
        "context",
        "tools",
        "tool_results",
        "provider_state",
    )
    assert all(
        provider_parameters[name].kind == inspect.Parameter.KEYWORD_ONLY
        for name in ("context", "tools", "tool_results", "provider_state")
    )


def test_task_and_job_composition_entry_points_remain_keyword_only() -> None:
    scheduler = inspect.signature(TaskScheduler.__init__).parameters
    assert tuple(scheduler) == (
        "self",
        "graph",
        "workers",
        "store",
        "events",
        "resources",
        "control",
        "max_concurrency",
        "agent_concurrency",
    )
    assert all(
        scheduler[name].kind == inspect.Parameter.KEYWORD_ONLY for name in tuple(scheduler)[1:]
    )

    jobs = inspect.signature(JobManager.__init__).parameters
    assert tuple(jobs) == (
        "self",
        "runtime",
        "allowed_worker_ids",
        "store",
        "events",
        "completion_notifier",
        "dead_letters",
    )
    assert all(jobs[name].kind == inspect.Parameter.KEYWORD_ONLY for name in tuple(jobs)[1:])


def test_legacy_result_schemas_keep_required_fields_and_status_values() -> None:
    input_schema = AgentInput.model_json_schema()
    response_schema = ModelResponse.model_json_schema()
    result_schema = AgentResult.model_json_schema()

    assert set(input_schema["required"]) == {"text"}
    assert {"text", "tool_calls", "provider_state", "usage"} <= set(response_schema["properties"])
    assert {"run_id", "session_id", "text", "status"} <= set(result_schema["required"])
    assert set(result_schema["properties"]["status"]["enum"]) == {"completed", "failed"}
