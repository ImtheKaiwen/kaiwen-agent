from __future__ import annotations

import asyncio

from kaiwen_agent.runtime import RuntimeIdentity, RuntimeRunRecord
from kaiwen_agent.toolkits import SnapshotBuilder


class WorkspaceFingerprintProvider:
    """Trusted source of the current content-aware project fingerprint for the gate.

    It reads the workspace through the same policy, limits, and exclusions as an
    execution snapshot (`SnapshotBuilder.fingerprint`), so a fingerprint taken here equals
    the fingerprint of a snapshot of the same bytes that a sandbox run is attributed to.
    Nothing a model supplies influences it. An incomplete or unsafe workspace raises
    `SnapshotError`, which makes the workflow controller fail closed.
    """

    def __init__(self, builder: SnapshotBuilder) -> None:
        self.builder = builder

    async def current(self, *, run: RuntimeRunRecord, identity: RuntimeIdentity) -> str:
        del run, identity  # the fingerprint depends only on the workspace bytes
        return await asyncio.to_thread(self.builder.fingerprint)
