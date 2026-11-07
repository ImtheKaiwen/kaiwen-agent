from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from kaiwen_agent.audit import AuditRecord
from kaiwen_agent.control.checkpoints import TaskCheckpoint
from kaiwen_agent.events import AgentEvent, EventScope, redact_agent_event
from kaiwen_agent.jobs.models import TERMINAL_JOB_STATUSES, AgentJob, JobStatus
from kaiwen_agent.lifecycle.models import (
    LifecycleCycle,
    LifecycleRuntimeRecord,
    LifecycleWakeup,
    WakeupStatus,
)
from kaiwen_agent.memory.models import SessionSummary
from kaiwen_agent.realtime.conversation import ConversationMessage
from kaiwen_agent.runtime.durability import RuntimeRunRecord, RuntimeRunStatus
from kaiwen_agent.sessions import SessionRecord
from kaiwen_agent.tasks.models import AgentTask, TaskStatus
from kaiwen_agent.tools.durability import (
    ActionIntentRecord,
    ActionIntentStatus,
    ApprovalStatus,
    DurableApprovalRecord,
)
from kaiwen_agent.types import AgentRun, ToolResult, utc_now

T = TypeVar("T")


class SQLitePersistence:
    """Dependency-free SQLite reference adapter for local and small deployments."""

    is_durable = True

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            await self._run(self._create_schema)
            self._initialized = True

    async def save_session(self, session: SessionRecord) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO sessions (id, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at""",
                (
                    session.id,
                    json.dumps(session.metadata, ensure_ascii=False, default=str),
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                ),
            )

        await self._run(operation)

    async def get_session(self, session_id: str) -> SessionRecord | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> SessionRecord | None:
            row = connection.execute(
                "SELECT id, metadata_json, created_at, updated_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return SessionRecord(
                id=row["id"],
                metadata=json.loads(row["metadata_json"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

        return await self._run(operation)

    async def save(self, run: AgentRun) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO runs (id, session_id, status, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    completed_at = excluded.completed_at""",
                (
                    run.id,
                    run.session_id,
                    run.status,
                    run.created_at.isoformat(),
                    run.completed_at.isoformat() if run.completed_at else None,
                ),
            )

        await self._run(operation)

    async def get(self, run_id: str) -> AgentRun | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> AgentRun | None:
            row = connection.execute(
                "SELECT id, session_id, status, created_at, completed_at FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            return AgentRun(
                id=row["id"],
                session_id=row["session_id"],
                status=row["status"],
                created_at=row["created_at"],
                completed_at=row["completed_at"],
            )

        return await self._run(operation)

    async def append(self, event: AgentEvent) -> None:
        await self.initialize()
        event = redact_agent_event(event)

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO events
                    (id, type, timestamp, trace_id, session_id,
                     tenant_id, user_id, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id,
                    event.type,
                    event.timestamp.isoformat(),
                    event.trace_id,
                    event.session_id,
                    event.tenant_id,
                    event.user_id,
                    json.dumps(event.payload, ensure_ascii=False, default=str),
                ),
            )

        await self._run(operation)

    async def list_events(
        self,
        *,
        session_id: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentEvent]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[AgentEvent]:
            after_sequence = 0
            if after_id is not None:
                cursor = connection.execute(
                    "SELECT sequence FROM events WHERE id = ? AND session_id = ?",
                    (after_id, session_id),
                ).fetchone()
                if cursor is None:
                    return []
                after_sequence = int(cursor["sequence"])
            rows = connection.execute(
                """SELECT id, type, timestamp, trace_id, session_id,
                    tenant_id, user_id, payload_json
                FROM events WHERE session_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?""",
                (session_id, after_sequence, limit),
            ).fetchall()
            return [
                AgentEvent(
                    id=row["id"],
                    type=row["type"],
                    timestamp=row["timestamp"],
                    trace_id=row["trace_id"],
                    session_id=row["session_id"],
                    tenant_id=row["tenant_id"],
                    user_id=row["user_id"],
                    payload=json.loads(row["payload_json"]),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def list_events_scoped(
        self,
        *,
        scope: EventScope,
        after_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentEvent]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[AgentEvent]:
            parameters = (scope.session_id, scope.tenant_id, scope.user_id)
            after_sequence = 0
            if after_id is not None:
                cursor = connection.execute(
                    """SELECT sequence FROM events
                    WHERE id = ? AND session_id = ? AND tenant_id = ? AND user_id = ?""",
                    (after_id, *parameters),
                ).fetchone()
                if cursor is None:
                    return []
                after_sequence = int(cursor["sequence"])
            rows = connection.execute(
                """SELECT id, type, timestamp, trace_id, session_id,
                    tenant_id, user_id, payload_json
                FROM events
                WHERE session_id = ? AND tenant_id = ? AND user_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?""",
                (*parameters, after_sequence, limit),
            ).fetchall()
            return [
                AgentEvent(
                    id=row["id"],
                    type=row["type"],
                    timestamp=row["timestamp"],
                    trace_id=row["trace_id"],
                    session_id=row["session_id"],
                    tenant_id=row["tenant_id"],
                    user_id=row["user_id"],
                    payload=json.loads(row["payload_json"]),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def append_audit(self, record: AuditRecord) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO audit_log
                    (id, trace_id, session_id, action, outcome, details_json, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.trace_id,
                    record.session_id,
                    record.action,
                    record.outcome,
                    json.dumps(record.details, ensure_ascii=False, default=str),
                    record.timestamp.isoformat(),
                ),
            )

        await self._run(operation)

    async def save_task(self, task: AgentTask) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO tasks
                    (id, status, agent_id, parent_id, task_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    agent_id = excluded.agent_id,
                    parent_id = excluded.parent_id,
                    task_json = excluded.task_json,
                    updated_at = excluded.updated_at""",
                (
                    task.id,
                    task.status,
                    task.agent_id,
                    task.parent_id,
                    task.model_dump_json(),
                    utc_now().isoformat(),
                ),
            )

        await self._run(operation)

    async def get_task(self, task_id: str) -> AgentTask | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> AgentTask | None:
            row = connection.execute(
                "SELECT task_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return AgentTask.model_validate_json(row["task_json"]) if row else None

        return await self._run(operation)

    async def append_message(self, message: ConversationMessage) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT OR IGNORE INTO conversation_messages
                    (id, tenant_id, conversation_id, user_id, message_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    message.id,
                    message.tenant_id,
                    message.conversation_id,
                    message.user_id,
                    message.model_dump_json(),
                    message.created_at.isoformat(),
                ),
            )

        await self._run(operation)

    async def list_messages(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> list[ConversationMessage]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[ConversationMessage]:
            after_sequence = 0
            if after_id is not None:
                cursor = connection.execute(
                    """SELECT sequence FROM conversation_messages
                    WHERE id = ? AND tenant_id = ? AND conversation_id = ?""",
                    (after_id, tenant_id, conversation_id),
                ).fetchone()
                if cursor is None:
                    return []
                after_sequence = int(cursor["sequence"])
            rows = connection.execute(
                """SELECT message_json FROM conversation_messages
                WHERE tenant_id = ? AND conversation_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?""",
                (tenant_id, conversation_id, after_sequence, limit),
            ).fetchall()
            return [ConversationMessage.model_validate_json(row["message_json"]) for row in rows]

        return await self._run(operation)

    async def list_tasks(self, *, status: TaskStatus | None = None) -> list[AgentTask]:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[AgentTask]:
            if status is None:
                rows = connection.execute(
                    "SELECT task_json FROM tasks ORDER BY rowid ASC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT task_json FROM tasks WHERE status = ? ORDER BY rowid ASC",
                    (status,),
                ).fetchall()
            return [AgentTask.model_validate_json(row["task_json"]) for row in rows]

        return await self._run(operation)

    async def save_checkpoint(self, checkpoint: TaskCheckpoint) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO task_checkpoints
                    (id, task_id, sequence, checkpoint_json, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    checkpoint.id,
                    checkpoint.task_id,
                    checkpoint.sequence,
                    checkpoint.model_dump_json(),
                    checkpoint.created_at.isoformat(),
                ),
            )

        await self._run(operation)

    async def latest_checkpoint(self, task_id: str) -> TaskCheckpoint | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> TaskCheckpoint | None:
            row = connection.execute(
                """SELECT checkpoint_json FROM task_checkpoints
                WHERE task_id = ? ORDER BY sequence DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
            return TaskCheckpoint.model_validate_json(row["checkpoint_json"]) if row else None

        return await self._run(operation)

    async def list_audit(self, *, trace_id: str | None = None) -> list[AuditRecord]:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[AuditRecord]:
            query = """SELECT id, trace_id, session_id, action, outcome,
                details_json, timestamp FROM audit_log"""
            parameters: tuple[str, ...] = ()
            if trace_id is not None:
                query += " WHERE trace_id = ?"
                parameters = (trace_id,)
            query += " ORDER BY sequence ASC"
            rows = connection.execute(query, parameters).fetchall()
            return [
                AuditRecord(
                    id=row["id"],
                    trace_id=row["trace_id"],
                    session_id=row["session_id"],
                    action=row["action"],
                    outcome=row["outcome"],
                    details=json.loads(row["details_json"]),
                    timestamp=row["timestamp"],
                )
                for row in rows
            ]

        return await self._run(operation)

    async def save_runtime(self, runtime: LifecycleRuntimeRecord) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO lifecycle_runtime (agent_id, runtime_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    runtime_json = excluded.runtime_json,
                    updated_at = excluded.updated_at""",
                (
                    runtime.agent_id,
                    runtime.model_dump_json(),
                    runtime.updated_at.isoformat(),
                ),
            )

        await self._run(operation)

    async def get_runtime(self, agent_id: str) -> LifecycleRuntimeRecord | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> LifecycleRuntimeRecord | None:
            row = connection.execute(
                "SELECT runtime_json FROM lifecycle_runtime WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            return LifecycleRuntimeRecord.model_validate_json(row["runtime_json"]) if row else None

        return await self._run(operation)

    async def save_cycle(self, cycle: LifecycleCycle) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO lifecycle_cycles
                    (id, agent_id, status, woke_at, cycle_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    cycle_json = excluded.cycle_json""",
                (
                    cycle.id,
                    cycle.agent_id,
                    cycle.status,
                    cycle.woke_at.isoformat(),
                    cycle.model_dump_json(),
                ),
            )

        await self._run(operation)

    async def get_cycle(self, cycle_id: str) -> LifecycleCycle | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> LifecycleCycle | None:
            row = connection.execute(
                "SELECT cycle_json FROM lifecycle_cycles WHERE id = ?", (cycle_id,)
            ).fetchone()
            return LifecycleCycle.model_validate_json(row["cycle_json"]) if row else None

        return await self._run(operation)

    async def list_cycles(self, *, agent_id: str, limit: int = 100) -> list[LifecycleCycle]:
        self._validate_lifecycle_limit(limit)
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[LifecycleCycle]:
            rows = connection.execute(
                """SELECT cycle_json FROM lifecycle_cycles
                WHERE agent_id = ? ORDER BY woke_at DESC LIMIT ?""",
                (agent_id, limit),
            ).fetchall()
            return [LifecycleCycle.model_validate_json(row["cycle_json"]) for row in rows]

        return await self._run(operation)

    async def save_wakeup(self, wakeup: LifecycleWakeup) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO lifecycle_wakeups
                    (id, agent_id, status, scheduled_at, replace_key, priority,
                     wakeup_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    scheduled_at = excluded.scheduled_at,
                    replace_key = excluded.replace_key,
                    priority = excluded.priority,
                    wakeup_json = excluded.wakeup_json,
                    updated_at = excluded.updated_at""",
                (
                    wakeup.id,
                    wakeup.agent_id,
                    wakeup.status,
                    wakeup.scheduled_at.isoformat(),
                    wakeup.replace_key,
                    wakeup.priority,
                    wakeup.model_dump_json(),
                    wakeup.updated_at.isoformat(),
                ),
            )

        await self._run(operation)

    async def schedule_wakeup(self, wakeup: LifecycleWakeup) -> list[LifecycleWakeup]:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[LifecycleWakeup]:
            connection.execute("BEGIN IMMEDIATE")
            cancelled: list[LifecycleWakeup] = []
            now = utc_now()
            if wakeup.replace_key is not None:
                rows = connection.execute(
                    """SELECT wakeup_json FROM lifecycle_wakeups
                    WHERE agent_id = ? AND replace_key = ? AND status = ?""",
                    (
                        wakeup.agent_id,
                        wakeup.replace_key,
                        WakeupStatus.SCHEDULED,
                    ),
                ).fetchall()
                for row in rows:
                    existing = LifecycleWakeup.model_validate_json(row["wakeup_json"])
                    existing.status = WakeupStatus.CANCELLED
                    existing.updated_at = now
                    connection.execute(
                        """UPDATE lifecycle_wakeups
                        SET status = ?, wakeup_json = ?, updated_at = ? WHERE id = ?""",
                        (
                            WakeupStatus.CANCELLED,
                            existing.model_dump_json(),
                            now.isoformat(),
                            existing.id,
                        ),
                    )
                    cancelled.append(existing)
            connection.execute(
                """INSERT INTO lifecycle_wakeups
                    (id, agent_id, status, scheduled_at, replace_key, priority,
                     wakeup_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    wakeup.id,
                    wakeup.agent_id,
                    wakeup.status,
                    wakeup.scheduled_at.astimezone(UTC).isoformat(),
                    wakeup.replace_key,
                    wakeup.priority,
                    wakeup.model_dump_json(),
                    wakeup.updated_at.isoformat(),
                ),
            )
            return cancelled

        return await self._run(operation)

    async def get_wakeup(self, wakeup_id: str) -> LifecycleWakeup | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> LifecycleWakeup | None:
            row = connection.execute(
                "SELECT wakeup_json FROM lifecycle_wakeups WHERE id = ?", (wakeup_id,)
            ).fetchone()
            return LifecycleWakeup.model_validate_json(row["wakeup_json"]) if row else None

        return await self._run(operation)

    async def claim_next_wakeup(
        self, *, agent_id: str, due_before: datetime
    ) -> LifecycleWakeup | None:
        if due_before.tzinfo is None:
            raise ValueError("due_before must be timezone-aware")
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> LifecycleWakeup | None:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT id, wakeup_json FROM lifecycle_wakeups
                WHERE agent_id = ? AND status = ? AND scheduled_at <= ?
                ORDER BY priority DESC, scheduled_at ASC LIMIT 1""",
                (
                    agent_id,
                    WakeupStatus.SCHEDULED,
                    due_before.astimezone(UTC).isoformat(),
                ),
            ).fetchone()
            if row is None:
                return None
            wakeup = LifecycleWakeup.model_validate_json(row["wakeup_json"])
            wakeup.status = WakeupStatus.CLAIMED
            wakeup.woke_at = due_before
            wakeup.updated_at = due_before
            updated = connection.execute(
                """UPDATE lifecycle_wakeups
                SET status = ?, wakeup_json = ?, updated_at = ?
                WHERE id = ? AND status = ?""",
                (
                    WakeupStatus.CLAIMED,
                    wakeup.model_dump_json(),
                    due_before.isoformat(),
                    wakeup.id,
                    WakeupStatus.SCHEDULED,
                ),
            )
            return wakeup if updated.rowcount == 1 else None

        return await self._run(operation)

    async def claim_wakeup(self, *, wakeup_id: str, claimed_at: datetime) -> LifecycleWakeup | None:
        if claimed_at.tzinfo is None:
            raise ValueError("claimed_at must be timezone-aware")
        claimed_at = claimed_at.astimezone(UTC)
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> LifecycleWakeup | None:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT wakeup_json FROM lifecycle_wakeups
                WHERE id = ? AND status = ? AND scheduled_at <= ?""",
                (wakeup_id, WakeupStatus.SCHEDULED, claimed_at.isoformat()),
            ).fetchone()
            if row is None:
                return None
            wakeup = LifecycleWakeup.model_validate_json(row["wakeup_json"])
            wakeup.status = WakeupStatus.CLAIMED
            wakeup.woke_at = claimed_at
            wakeup.updated_at = claimed_at
            connection.execute(
                """UPDATE lifecycle_wakeups
                SET status = ?, wakeup_json = ?, updated_at = ? WHERE id = ?""",
                (
                    WakeupStatus.CLAIMED,
                    wakeup.model_dump_json(),
                    claimed_at.isoformat(),
                    wakeup.id,
                ),
            )
            return wakeup

        return await self._run(operation)

    async def cancel_scheduled_wakeups(
        self, *, agent_id: str, replace_key: str
    ) -> list[LifecycleWakeup]:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[LifecycleWakeup]:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT wakeup_json FROM lifecycle_wakeups
                WHERE agent_id = ? AND replace_key = ? AND status = ?""",
                (agent_id, replace_key, WakeupStatus.SCHEDULED),
            ).fetchall()
            cancelled: list[LifecycleWakeup] = []
            now = utc_now()
            for row in rows:
                wakeup = LifecycleWakeup.model_validate_json(row["wakeup_json"])
                wakeup.status = WakeupStatus.CANCELLED
                wakeup.updated_at = now
                connection.execute(
                    """UPDATE lifecycle_wakeups
                    SET status = ?, wakeup_json = ?, updated_at = ? WHERE id = ?""",
                    (
                        WakeupStatus.CANCELLED,
                        wakeup.model_dump_json(),
                        now.isoformat(),
                        wakeup.id,
                    ),
                )
                cancelled.append(wakeup)
            return cancelled

        return await self._run(operation)

    async def list_wakeups(
        self,
        *,
        agent_id: str,
        status: WakeupStatus | None = None,
        due_before: datetime | None = None,
        limit: int = 100,
    ) -> list[LifecycleWakeup]:
        self._validate_lifecycle_limit(limit)
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[LifecycleWakeup]:
            clauses = ["agent_id = ?"]
            parameters: list[str | int] = [agent_id]
            if status is not None:
                clauses.append("status = ?")
                parameters.append(status)
            if due_before is not None:
                if due_before.tzinfo is None:
                    raise ValueError("due_before must be timezone-aware")
                clauses.append("scheduled_at <= ?")
                parameters.append(due_before.isoformat())
            parameters.append(limit)
            rows = connection.execute(
                f"""SELECT wakeup_json FROM lifecycle_wakeups
                WHERE {" AND ".join(clauses)}
                ORDER BY scheduled_at ASC, priority DESC LIMIT ?""",
                parameters,
            ).fetchall()
            return [LifecycleWakeup.model_validate_json(row["wakeup_json"]) for row in rows]

        return await self._run(operation)

    async def save_session_summary(self, summary: SessionSummary) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO session_summaries
                    (tenant_id, user_id, conversation_id, summary_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, user_id, conversation_id) DO UPDATE SET
                    summary_json = excluded.summary_json,
                    updated_at = excluded.updated_at""",
                (
                    summary.tenant_id,
                    summary.user_id,
                    summary.conversation_id,
                    summary.model_dump_json(),
                    summary.updated_at.isoformat(),
                ),
            )

        await self._run(operation)

    async def get_session_summary(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> SessionSummary | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> SessionSummary | None:
            row = connection.execute(
                """SELECT summary_json FROM session_summaries
                WHERE tenant_id = ? AND user_id = ? AND conversation_id = ?""",
                (tenant_id, user_id, conversation_id),
            ).fetchone()
            return SessionSummary.model_validate_json(row["summary_json"]) if row else None

        return await self._run(operation)

    async def delete_session_summary(
        self, *, tenant_id: str, user_id: str, conversation_id: str
    ) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """DELETE FROM session_summaries
                WHERE tenant_id = ? AND user_id = ? AND conversation_id = ?""",
                (tenant_id, user_id, conversation_id),
            )

        await self._run(operation)

    async def save_job(self, job: AgentJob) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO agent_jobs
                    (id, status, tenant_id, user_id, conversation_id, parent_job_id,
                     priority, job_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    tenant_id = excluded.tenant_id,
                    user_id = excluded.user_id,
                    conversation_id = excluded.conversation_id,
                    parent_job_id = excluded.parent_job_id,
                    priority = excluded.priority,
                    job_json = excluded.job_json,
                    updated_at = excluded.updated_at""",
                (
                    job.id,
                    job.status,
                    job.tenant_id,
                    job.user_id,
                    job.conversation_id,
                    job.parent_job_id,
                    job.priority,
                    job.model_dump_json(),
                    job.updated_at.isoformat(),
                ),
            )

        await self._run(operation)

    async def get_job(self, job_id: str) -> AgentJob | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> AgentJob | None:
            row = connection.execute(
                "SELECT job_json FROM agent_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return AgentJob.model_validate_json(row["job_json"]) if row else None

        return await self._run(operation)

    async def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        conversation_id: str | None = None,
        parent_job_id: str | None = None,
        status: JobStatus | None = None,
        limit: int = 100,
    ) -> list[AgentJob]:
        self._validate_job_limit(limit)
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[AgentJob]:
            clauses: list[str] = []
            parameters: list[str | int] = []
            filters = {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "parent_job_id": parent_job_id,
                "status": status,
            }
            for column, value in filters.items():
                if value is not None:
                    clauses.append(f"{column} = ?")
                    parameters.append(value)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            parameters.append(limit)
            rows = connection.execute(
                f"""SELECT job_json FROM agent_jobs {where}
                ORDER BY priority DESC, updated_at DESC LIMIT ?""",
                parameters,
            ).fetchall()
            return [AgentJob.model_validate_json(row["job_json"]) for row in rows]

        return await self._run(operation)

    async def count_jobs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        requested_by_agent_id: str | None = None,
        parent_run_id: str | None = None,
        active_only: bool = False,
    ) -> int:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> int:
            clauses = ["tenant_id = ?", "user_id = ?"]
            parameters: list[str] = [tenant_id, user_id]
            if requested_by_agent_id is not None:
                clauses.append("json_extract(job_json, '$.requested_by_agent_id') = ?")
                parameters.append(requested_by_agent_id)
            if parent_run_id is not None:
                clauses.append("json_extract(job_json, '$.parent_run_id') = ?")
                parameters.append(parent_run_id)
            if active_only:
                placeholders = ",".join("?" for _ in TERMINAL_JOB_STATUSES)
                clauses.append(f"status NOT IN ({placeholders})")
                parameters.extend(str(status) for status in TERMINAL_JOB_STATUSES)
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM agent_jobs WHERE {' AND '.join(clauses)}",
                parameters,
            ).fetchone()
            return int(row["count"])

        return await self._run(operation)

    async def claim_job(self, job_id: str) -> AgentJob | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> AgentJob | None:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT job_json FROM agent_jobs WHERE id = ? AND status = ?",
                (job_id, JobStatus.QUEUED),
            ).fetchone()
            if row is None:
                return None
            job = AgentJob.model_validate_json(row["job_json"])
            now = utc_now()
            claimed = job.model_copy(
                update={
                    "status": JobStatus.RUNNING,
                    "started_at": job.started_at or now,
                    "updated_at": now,
                }
            )
            updated = connection.execute(
                """UPDATE agent_jobs SET status = ?, job_json = ?, updated_at = ?
                WHERE id = ? AND status = ?""",
                (
                    JobStatus.RUNNING,
                    claimed.model_dump_json(),
                    now.isoformat(),
                    job_id,
                    JobStatus.QUEUED,
                ),
            )
            return claimed if updated.rowcount == 1 else None

        return await self._run(operation)

    async def requeue_recoverable_jobs(self, *, limit: int = 100) -> list[AgentJob]:
        self._validate_job_limit(limit)
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[AgentJob]:
            connection.execute("BEGIN IMMEDIATE")
            excluded = [*TERMINAL_JOB_STATUSES, JobStatus.CANCEL_REQUESTED]
            placeholders = ",".join("?" for _ in excluded)
            rows = connection.execute(
                f"""SELECT id, job_json FROM agent_jobs
                WHERE status NOT IN ({placeholders})
                ORDER BY priority DESC, updated_at ASC LIMIT ?""",
                [*excluded, limit],
            ).fetchall()
            now = utc_now()
            requeued: list[AgentJob] = []
            for row in rows:
                job = AgentJob.model_validate_json(row["job_json"])
                updated = job.model_copy(
                    update={
                        "status": JobStatus.QUEUED,
                        "queued_at": now,
                        "started_at": None,
                        "updated_at": now,
                    }
                )
                connection.execute(
                    """UPDATE agent_jobs SET status = ?, job_json = ?, updated_at = ?
                    WHERE id = ?""",
                    (
                        JobStatus.QUEUED,
                        updated.model_dump_json(),
                        now.isoformat(),
                        updated.id,
                    ),
                )
                requeued.append(updated)
            return requeued

        return await self._run(operation)

    async def finalize_cancel_requested_jobs(self, *, limit: int = 100) -> list[AgentJob]:
        self._validate_job_limit(limit)
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[AgentJob]:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT id, job_json FROM agent_jobs WHERE status = ?
                ORDER BY updated_at ASC LIMIT ?""",
                (JobStatus.CANCEL_REQUESTED, limit),
            ).fetchall()
            now = utc_now()
            cancelled: list[AgentJob] = []
            for row in rows:
                job = AgentJob.model_validate_json(row["job_json"])
                updated = job.model_copy(
                    update={
                        "status": JobStatus.CANCELLED,
                        "completed_at": now,
                        "updated_at": now,
                    }
                )
                connection.execute(
                    """UPDATE agent_jobs SET status = ?, job_json = ?, updated_at = ?
                    WHERE id = ? AND status = ?""",
                    (
                        JobStatus.CANCELLED,
                        updated.model_dump_json(),
                        now.isoformat(),
                        updated.id,
                        JobStatus.CANCEL_REQUESTED,
                    ),
                )
                cancelled.append(updated)
            return cancelled

        return await self._run(operation)

    async def save_runtime_run(self, run: RuntimeRunRecord) -> None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT run_json FROM runtime_runs WHERE id = ?", (run.id,)
            ).fetchone()
            if row is not None:
                current = RuntimeRunRecord.model_validate_json(row["run_json"])
                if (
                    current.tenant_id,
                    current.user_id,
                    current.session_id,
                    current.conversation_id,
                    current.mode,
                    current.autonomy,
                    current.version_pins,
                ) != (
                    run.tenant_id,
                    run.user_id,
                    run.session_id,
                    run.conversation_id,
                    run.mode,
                    run.autonomy,
                    run.version_pins,
                ):
                    raise ValueError("Runtime run identity and version pins are immutable")
                _validate_runtime_run_transition(current.status, run.status)
            connection.execute(
                """INSERT INTO runtime_runs
                    (id, tenant_id, user_id, status, run_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    run_json = excluded.run_json,
                    updated_at = excluded.updated_at""",
                (
                    run.id,
                    run.tenant_id,
                    run.user_id,
                    run.status,
                    run.model_dump_json(),
                    run.updated_at.isoformat(),
                ),
            )

        await self._run(operation)

    async def get_runtime_run(
        self, *, run_id: str, tenant_id: str, user_id: str
    ) -> RuntimeRunRecord | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> RuntimeRunRecord | None:
            row = connection.execute(
                """SELECT run_json FROM runtime_runs
                WHERE id = ? AND tenant_id = ? AND user_id = ?""",
                (run_id, tenant_id, user_id),
            ).fetchone()
            return RuntimeRunRecord.model_validate_json(row["run_json"]) if row else None

        return await self._run(operation)

    async def list_runtime_runs(
        self, *, statuses: frozenset[RuntimeRunStatus], limit: int = 100
    ) -> list[RuntimeRunRecord]:
        _validate_runtime_limit(limit)
        if not statuses:
            return []
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[RuntimeRunRecord]:
            placeholders = ",".join("?" for _ in statuses)
            rows = connection.execute(
                f"""SELECT run_json FROM runtime_runs
                WHERE status IN ({placeholders}) ORDER BY updated_at ASC LIMIT ?""",
                [*(str(status) for status in statuses), limit],
            ).fetchall()
            return [RuntimeRunRecord.model_validate_json(row["run_json"]) for row in rows]

        return await self._run(operation)

    async def prepare_action_intent(self, intent: ActionIntentRecord) -> ActionIntentRecord:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> ActionIntentRecord:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO action_intents
                    (id, runtime_run_id, tenant_id, user_id, status,
                     idempotency_key, intent_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    intent.id,
                    intent.runtime_run_id,
                    intent.tenant_id,
                    intent.user_id,
                    intent.status,
                    intent.idempotency_key,
                    intent.model_dump_json(),
                    intent.updated_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT intent_json FROM action_intents WHERE id = ?", (intent.id,)
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """SELECT intent_json FROM action_intents
                    WHERE tenant_id = ? AND user_id = ? AND runtime_run_id = ?
                        AND idempotency_key = ?""",
                    (
                        intent.tenant_id,
                        intent.user_id,
                        intent.runtime_run_id,
                        intent.idempotency_key,
                    ),
                ).fetchone()
            if row is None:
                raise RuntimeError("Action intent could not be persisted")
            stored = ActionIntentRecord.model_validate_json(row["intent_json"])
            _validate_action_binding(stored, intent)
            return stored

        return await self._run(operation)

    async def get_action_intent(
        self, *, intent_id: str, tenant_id: str, user_id: str
    ) -> ActionIntentRecord | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> ActionIntentRecord | None:
            row = connection.execute(
                """SELECT intent_json FROM action_intents
                WHERE id = ? AND tenant_id = ? AND user_id = ?""",
                (intent_id, tenant_id, user_id),
            ).fetchone()
            return ActionIntentRecord.model_validate_json(row["intent_json"]) if row else None

        return await self._run(operation)

    async def transition_action_intent(
        self,
        *,
        intent_id: str,
        tenant_id: str,
        user_id: str,
        expected: frozenset[ActionIntentStatus],
        status: ActionIntentStatus,
        result: ToolResult | None = None,
        error_code: str | None = None,
        external_receipt: dict[str, object] | None = None,
    ) -> ActionIntentRecord | None:
        _validate_action_transition_request(expected, status)
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> ActionIntentRecord | None:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT intent_json FROM action_intents
                WHERE id = ? AND tenant_id = ? AND user_id = ?""",
                (intent_id, tenant_id, user_id),
            ).fetchone()
            if row is None:
                return None
            current = ActionIntentRecord.model_validate_json(row["intent_json"])
            if current.status not in expected:
                return None
            updated = current.model_copy(
                update={
                    "status": status,
                    "result": result,
                    "error_code": error_code,
                    "external_receipt": external_receipt or {},
                    "updated_at": utc_now(),
                }
            )
            changed = connection.execute(
                """UPDATE action_intents SET status = ?, intent_json = ?, updated_at = ?
                WHERE id = ? AND tenant_id = ? AND user_id = ? AND status = ?""",
                (
                    status,
                    updated.model_dump_json(),
                    updated.updated_at.isoformat(),
                    intent_id,
                    tenant_id,
                    user_id,
                    current.status,
                ),
            )
            return updated if changed.rowcount == 1 else None

        return await self._run(operation)

    async def list_action_intents(
        self, *, statuses: frozenset[ActionIntentStatus], limit: int = 100
    ) -> list[ActionIntentRecord]:
        _validate_runtime_limit(limit)
        if not statuses:
            return []
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> list[ActionIntentRecord]:
            placeholders = ",".join("?" for _ in statuses)
            rows = connection.execute(
                f"""SELECT intent_json FROM action_intents
                WHERE status IN ({placeholders}) ORDER BY updated_at ASC LIMIT ?""",
                [*(str(status) for status in statuses), limit],
            ).fetchall()
            return [ActionIntentRecord.model_validate_json(row["intent_json"]) for row in rows]

        return await self._run(operation)

    async def create_pending_approval(
        self, approval: DurableApprovalRecord
    ) -> DurableApprovalRecord:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> DurableApprovalRecord:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO durable_approvals
                    (id, action_intent_id, tenant_id, user_id, status,
                     approval_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval.id,
                    approval.action_intent_id,
                    approval.tenant_id,
                    approval.user_id,
                    approval.status,
                    approval.model_dump_json(),
                    approval.updated_at.isoformat(),
                ),
            )
            row = connection.execute(
                """SELECT approval_json FROM durable_approvals
                WHERE action_intent_id = ? AND tenant_id = ? AND user_id = ?""",
                (approval.action_intent_id, approval.tenant_id, approval.user_id),
            ).fetchone()
            assert row is not None
            stored = DurableApprovalRecord.model_validate_json(row["approval_json"])
            _validate_approval_binding(stored, approval)
            return stored

        return await self._run(operation)

    async def get_approval(
        self, *, approval_id: str, tenant_id: str, user_id: str
    ) -> DurableApprovalRecord | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> DurableApprovalRecord | None:
            row = connection.execute(
                """SELECT approval_json FROM durable_approvals
                WHERE id = ? AND tenant_id = ? AND user_id = ?""",
                (approval_id, tenant_id, user_id),
            ).fetchone()
            return DurableApprovalRecord.model_validate_json(row["approval_json"]) if row else None

        return await self._run(operation)

    async def get_approval_for_intent(
        self, *, action_intent_id: str, tenant_id: str, user_id: str
    ) -> DurableApprovalRecord | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> DurableApprovalRecord | None:
            row = connection.execute(
                """SELECT approval_json FROM durable_approvals
                WHERE action_intent_id = ? AND tenant_id = ? AND user_id = ?""",
                (action_intent_id, tenant_id, user_id),
            ).fetchone()
            return DurableApprovalRecord.model_validate_json(row["approval_json"]) if row else None

        return await self._run(operation)

    async def resolve_approval(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        conversation_id: str | None,
        action_intent_id: str,
        tool_name: str,
        call_id: str,
        decided_by: str,
        approved: bool,
    ) -> DurableApprovalRecord | None:
        target = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
        return await self._transition_approval(
            approval_id=approval_id,
            tenant_id=tenant_id,
            user_id=user_id,
            expected=ApprovalStatus.PENDING,
            status=target,
            decided_by=decided_by,
            expected_binding=(conversation_id, action_intent_id, tool_name, call_id),
        )

    async def consume_approval(
        self, *, approval_id: str, tenant_id: str, user_id: str
    ) -> DurableApprovalRecord | None:
        return await self._transition_approval(
            approval_id=approval_id,
            tenant_id=tenant_id,
            user_id=user_id,
            expected=ApprovalStatus.APPROVED,
            status=ApprovalStatus.CONSUMED,
            decided_by=None,
            expected_binding=None,
        )

    async def _transition_approval(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        expected: ApprovalStatus,
        status: ApprovalStatus,
        decided_by: str | None,
        expected_binding: tuple[str | None, str, str, str] | None,
    ) -> DurableApprovalRecord | None:
        await self.initialize()

        def operation(connection: sqlite3.Connection) -> DurableApprovalRecord | None:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT approval_json FROM durable_approvals
                WHERE id = ? AND tenant_id = ? AND user_id = ? AND status = ?""",
                (approval_id, tenant_id, user_id, expected),
            ).fetchone()
            if row is None:
                return None
            current = DurableApprovalRecord.model_validate_json(row["approval_json"])
            if (
                expected_binding is not None
                and (
                    current.conversation_id,
                    current.action_intent_id,
                    current.tool_name,
                    current.call_id,
                )
                != expected_binding
            ):
                return None
            now = utc_now()
            updates: dict[str, object] = {"status": status, "updated_at": now}
            if status in {ApprovalStatus.APPROVED, ApprovalStatus.DENIED}:
                updates.update({"decided_by": decided_by, "decided_at": now})
            if status == ApprovalStatus.CONSUMED:
                updates["consumed_at"] = now
            updated = current.model_copy(update=updates)
            changed = connection.execute(
                """UPDATE durable_approvals
                SET status = ?, approval_json = ?, updated_at = ?
                WHERE id = ? AND tenant_id = ? AND user_id = ? AND status = ?""",
                (
                    status,
                    updated.model_dump_json(),
                    now.isoformat(),
                    approval_id,
                    tenant_id,
                    user_id,
                    expected,
                ),
            )
            return updated if changed.rowcount == 1 else None

        return await self._run(operation)

    @staticmethod
    def _validate_lifecycle_limit(limit: int) -> None:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")

    @staticmethod
    def _validate_job_limit(limit: int) -> None:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")

    async def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        def execute() -> T:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                result = operation(connection)
                connection.commit()
                return result
            finally:
                connection.close()

        return await asyncio.to_thread(execute)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE, type TEXT NOT NULL, timestamp TEXT NOT NULL,
                trace_id TEXT NOT NULL, session_id TEXT NOT NULL,
                tenant_id TEXT, user_id TEXT, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_session_sequence
                ON events(session_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id);
            CREATE TABLE IF NOT EXISTS audit_log (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE, trace_id TEXT NOT NULL, session_id TEXT NOT NULL,
                action TEXT NOT NULL, outcome TEXT NOT NULL,
                details_json TEXT NOT NULL, timestamp TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_trace_sequence
                ON audit_log(trace_id, sequence);
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, agent_id TEXT NOT NULL,
                parent_id TEXT, task_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
            CREATE TABLE IF NOT EXISTS conversation_messages (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, user_id TEXT NOT NULL,
                message_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conversation_messages_scope_sequence
                ON conversation_messages(tenant_id, conversation_id, sequence);
            CREATE TABLE IF NOT EXISTS task_checkpoints (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                checkpoint_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(task_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_checkpoints_task_sequence
                ON task_checkpoints(task_id, sequence);
            CREATE TABLE IF NOT EXISTS lifecycle_runtime (
                agent_id TEXT PRIMARY KEY, runtime_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lifecycle_cycles (
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, status TEXT NOT NULL,
                woke_at TEXT NOT NULL, cycle_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lifecycle_cycles_agent_woke
                ON lifecycle_cycles(agent_id, woke_at DESC);
            CREATE TABLE IF NOT EXISTS lifecycle_wakeups (
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, status TEXT NOT NULL,
                scheduled_at TEXT NOT NULL, replace_key TEXT, priority INTEGER NOT NULL,
                wakeup_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lifecycle_wakeups_due
                ON lifecycle_wakeups(agent_id, status, scheduled_at, priority);
            CREATE INDEX IF NOT EXISTS idx_lifecycle_wakeups_replace
                ON lifecycle_wakeups(agent_id, replace_key, status);
            CREATE TABLE IF NOT EXISTS session_summaries (
                tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, summary_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, user_id, conversation_id)
            );
            CREATE INDEX IF NOT EXISTS idx_session_summaries_updated
                ON session_summaries(updated_at);
            CREATE TABLE IF NOT EXISTS agent_jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL,
                tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                conversation_id TEXT, parent_job_id TEXT,
                priority INTEGER NOT NULL, job_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_jobs_owner_updated
                ON agent_jobs(tenant_id, user_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_agent_jobs_status_priority
                ON agent_jobs(status, priority DESC, updated_at);
            CREATE INDEX IF NOT EXISTS idx_agent_jobs_parent
                ON agent_jobs(parent_job_id, updated_at);
            CREATE TABLE IF NOT EXISTS runtime_runs (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                status TEXT NOT NULL, run_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_runtime_runs_status_updated
                ON runtime_runs(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_runs_owner_updated
                ON runtime_runs(tenant_id, user_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS action_intents (
                id TEXT PRIMARY KEY, runtime_run_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                status TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                intent_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, user_id, runtime_run_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_action_intents_status_updated
                ON action_intents(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_action_intents_run
                ON action_intents(runtime_run_id, updated_at);
            CREATE TABLE IF NOT EXISTS durable_approvals (
                id TEXT PRIMARY KEY, action_intent_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                status TEXT NOT NULL, approval_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(action_intent_id, tenant_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_durable_approvals_owner_status
                ON durable_approvals(tenant_id, user_id, status, updated_at);
            """
        )
        event_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(events)").fetchall()
        }
        if "tenant_id" not in event_columns:
            connection.execute("ALTER TABLE events ADD COLUMN tenant_id TEXT")
        if "user_id" not in event_columns:
            connection.execute("ALTER TABLE events ADD COLUMN user_id TEXT")
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_events_owner_session_sequence
            ON events(tenant_id, user_id, session_id, sequence)"""
        )


def _validate_runtime_limit(limit: int) -> None:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")


def _validate_runtime_run_transition(current: RuntimeRunStatus, target: RuntimeRunStatus) -> None:
    allowed = {
        RuntimeRunStatus.CREATED: {
            RuntimeRunStatus.CREATED,
            RuntimeRunStatus.RUNNING,
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.CANCELLED,
        },
        RuntimeRunStatus.RUNNING: {
            RuntimeRunStatus.RUNNING,
            RuntimeRunStatus.PAUSED,
            RuntimeRunStatus.BLOCKED,
            RuntimeRunStatus.COMPLETED,
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.CANCELLED,
        },
        RuntimeRunStatus.PAUSED: {
            RuntimeRunStatus.PAUSED,
            RuntimeRunStatus.RUNNING,
            RuntimeRunStatus.BLOCKED,
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.CANCELLED,
        },
        RuntimeRunStatus.BLOCKED: {
            RuntimeRunStatus.BLOCKED,
            RuntimeRunStatus.RUNNING,
            RuntimeRunStatus.CANCELLED,
            RuntimeRunStatus.FAILED,
        },
        RuntimeRunStatus.COMPLETED: {RuntimeRunStatus.COMPLETED},
        RuntimeRunStatus.FAILED: {RuntimeRunStatus.FAILED},
        RuntimeRunStatus.CANCELLED: {RuntimeRunStatus.CANCELLED},
    }
    if target not in allowed[current]:
        raise ValueError(f"Invalid runtime run transition: {current} -> {target}")


def _validate_action_binding(stored: ActionIntentRecord, requested: ActionIntentRecord) -> None:
    immutable_fields = (
        "runtime_run_id",
        "tenant_id",
        "user_id",
        "conversation_id",
        "tool_name",
        "call_id",
        "arguments_checksum",
        "idempotency_key",
        "side_effect",
    )
    if any(getattr(stored, field) != getattr(requested, field) for field in immutable_fields):
        raise ValueError("Action intent identity was reused with a different binding")


def _validate_action_transition_request(
    expected: frozenset[ActionIntentStatus], target: ActionIntentStatus
) -> None:
    if not expected:
        raise ValueError("expected action states must not be empty")
    allowed = {
        ActionIntentStatus.PREPARED: {
            ActionIntentStatus.EXECUTING,
            ActionIntentStatus.CANCELLED,
        },
        ActionIntentStatus.EXECUTING: {
            ActionIntentStatus.SUCCEEDED,
            ActionIntentStatus.FAILED,
            ActionIntentStatus.UNCERTAIN,
        },
        ActionIntentStatus.UNCERTAIN: {
            ActionIntentStatus.SUCCEEDED,
            ActionIntentStatus.FAILED,
            ActionIntentStatus.CANCELLED,
        },
    }
    if any(target not in allowed.get(current, set()) for current in expected):
        raise ValueError(f"Invalid action transition to {target} from {sorted(expected)}")


def _validate_approval_binding(
    stored: DurableApprovalRecord, requested: DurableApprovalRecord
) -> None:
    immutable_fields = (
        "action_intent_id",
        "runtime_run_id",
        "tenant_id",
        "user_id",
        "conversation_id",
        "tool_name",
        "call_id",
        "arguments_checksum",
    )
    if any(getattr(stored, field) != getattr(requested, field) for field in immutable_fields):
        raise ValueError("Approval identity was reused with a different binding")
