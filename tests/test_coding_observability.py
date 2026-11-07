from __future__ import annotations

import asyncio

import pytest

from kaiwen_agent.artifacts import (
    ArtifactContext,
    ArtifactManager,
    ArtifactNotFoundError,
)
from kaiwen_agent.events import EventScope, InMemoryEventSink
from kaiwen_agent.kits.coding import (
    CodingEventEmitter,
    CodingRunManifest,
    persist_run_manifest,
)
from kaiwen_agent.runtime import RuntimeIdentity, RuntimeVersionPins


def _pins() -> RuntimeVersionPins:
    return RuntimeVersionPins(
        workspace_id="workspace",
        workspace_version=1,
        agent_id="coding.solo",
        agent_version=1,
        prompt_id="coding.solo",
        prompt_version=1,
        prompt_checksum="a" * 64,
        capability_manifest_id="coding.solo",
        capability_manifest_version=1,
        provider_ref="fake",
        tool_names=(),
        toolset_checksum="b" * 64,
    )


def test_manifest_and_events_are_owner_scoped_and_redacted() -> None:
    async def scenario() -> None:
        artifacts = ArtifactManager()
        owner = ArtifactContext(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            trace_id="trace-1",
            run_id="run-1",
        )
        other = owner.model_copy(update={"user_id": "user-2"})
        manifest = CodingRunManifest(
            kit_version=1,
            prompt_checksums={"coding.solo@1": "a" * 64},
            version_pins=_pins(),
            plan_checksum="c" * 64,
            workspace_id="workspace",
            project_fingerprint="d" * 64,
        )
        artifact_id = await persist_run_manifest(manifest, artifacts=artifacts, context=owner)
        assert (await artifacts.read(owner, artifact_id)).metadata.run_id == "run-1"
        with pytest.raises(ArtifactNotFoundError):
            await artifacts.read(other, artifact_id)

        events = InMemoryEventSink()
        emitter = CodingEventEmitter(events)
        identity = RuntimeIdentity(
            session_id="session-1",
            tenant_id="tenant-1",
            user_id="user-1",
            trace_id="trace-1",
        )
        await emitter.emit(
            "coding.command.completed",
            identity=identity,
            payload={"exit_code": 0, "authorization": "Bearer secret-value"},
        )
        scoped = await events.list_events_scoped(
            scope=EventScope(tenant_id="tenant-1", user_id="user-1", session_id="session-1")
        )
        assert scoped[0].type == "tool.completed"
        assert scoped[0].payload["coding_event"] == "coding.command.completed"
        assert "secret-value" not in str(scoped[0].payload)
        assert (
            await events.list_events_scoped(
                scope=EventScope(tenant_id="tenant-1", user_id="user-2", session_id="session-1")
            )
            == []
        )

    asyncio.run(scenario())
