from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from typing import Any

from kaiwen_agent.memory.models import (
    MemoryItem,
    MemorySearchQuery,
    MemorySearchResult,
    MemoryStatus,
)
from kaiwen_agent.types import utc_now


class PostgreSQLMemoryStore:
    """Optional production long-term-memory adapter backed by PostgreSQL + pgvector."""

    def __init__(
        self,
        connection_info: str,
        *,
        embedding_dimensions: int,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        if not connection_info.strip():
            raise ValueError("connection_info cannot be empty")
        if not 1 <= embedding_dimensions <= 16_000:
            raise ValueError("embedding_dimensions must be between 1 and 16000")
        if not 1 <= min_size <= max_size:
            raise ValueError("pool sizes must satisfy 1 <= min_size <= max_size")
        try:
            pool_module = importlib.import_module("psycopg_pool")
            vector_module = importlib.import_module("pgvector.psycopg")
            rows_module = importlib.import_module("psycopg.rows")
        except ImportError as error:
            raise RuntimeError(
                "PostgreSQLMemoryStore requires kaiwen-agent[postgres-memory]"
            ) from error

        register_vector_async = vector_module.register_vector_async

        async def configure(connection: Any) -> None:
            await register_vector_async(connection)

        self.embedding_dimensions = embedding_dimensions
        self._pool: Any = pool_module.AsyncConnectionPool(
            connection_info,
            min_size=min_size,
            max_size=max_size,
            open=False,
            configure=configure,
            kwargs={"row_factory": rows_module.dict_row},
        )
        self._initialized = False

    async def open(self) -> None:
        await self._pool.open()
        await self.initialize()

    async def close(self) -> None:
        await self._pool.close()

    async def __aenter__(self) -> PostgreSQLMemoryStore:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with (
            self._pool.connection() as connection,
            connection.cursor() as cursor,
        ):
            await cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await cursor.execute(
                f"""CREATE TABLE IF NOT EXISTS memory_items (
                        id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        namespace TEXT NOT NULL,
                        type TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        key TEXT NOT NULL,
                        content TEXT NOT NULL,
                        structured_data JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        confidence DOUBLE PRECISION NOT NULL,
                        importance DOUBLE PRECISION NOT NULL,
                        sensitivity TEXT NOT NULL,
                        status TEXT NOT NULL,
                        supersedes_id TEXT REFERENCES memory_items(id),
                        source_type TEXT NOT NULL,
                        source_conversation_id TEXT,
                        source_message_id TEXT,
                        source_run_id TEXT,
                        source_job_id TEXT,
                        embedding vector({self.embedding_dimensions}),
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL,
                        last_accessed_at TIMESTAMPTZ,
                        expires_at TIMESTAMPTZ
                    )"""
            )
            await cursor.execute(
                """CREATE INDEX IF NOT EXISTS idx_memory_owner_status
                    ON memory_items(tenant_id, user_id, status, updated_at DESC)"""
            )
            await cursor.execute(
                """CREATE INDEX IF NOT EXISTS idx_memory_namespace
                ON memory_items(tenant_id, user_id, namespace, status)"""
            )
            await cursor.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_active_key
                ON memory_items(tenant_id, user_id, namespace, subject, key)
                WHERE status = 'active'"""
            )
            await cursor.execute(
                """CREATE INDEX IF NOT EXISTS idx_memory_lexical
                    ON memory_items USING GIN (
                        to_tsvector('simple', subject || ' ' || key || ' ' || content)
                    )"""
            )
            await cursor.execute(
                """CREATE INDEX IF NOT EXISTS idx_memory_embedding
                    ON memory_items USING hnsw (embedding vector_cosine_ops)"""
            )
        self._initialized = True

    async def save_memory(self, item: MemoryItem) -> MemoryItem:
        await self.initialize()
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
        ):
            scope_key = "\x1f".join(
                (item.tenant_id, item.user_id, item.namespace, item.subject, item.key)
            )
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (scope_key,)
            )
            active_cursor = await connection.execute(
                """SELECT * FROM memory_items
                WHERE tenant_id=%s AND user_id=%s AND namespace=%s
                  AND subject=%s AND key=%s AND status=%s
                FOR UPDATE""",
                (
                    item.tenant_id,
                    item.user_id,
                    item.namespace,
                    item.subject,
                    item.key,
                    MemoryStatus.ACTIVE,
                ),
            )
            active_row = await active_cursor.fetchone()
            if active_row is not None and item.status == MemoryStatus.ACTIVE:
                active = self._row_to_item(active_row)
                if active.content == item.content:
                    return active
                if item.supersedes_id not in {None, active.id}:
                    raise ValueError("Concurrent memory conflict requires a fresh admission")
                item = item.model_copy(update={"supersedes_id": active.id})
            if item.supersedes_id is not None and item.status == MemoryStatus.ACTIVE:
                updated = await connection.execute(
                    """UPDATE memory_items SET status = %s, updated_at = %s
                        WHERE id = %s AND tenant_id = %s AND user_id = %s""",
                    (
                        MemoryStatus.SUPERSEDED,
                        utc_now(),
                        item.supersedes_id,
                        item.tenant_id,
                        item.user_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError("Superseded memory is outside the trusted owner scope")
            await connection.execute(self._insert_sql(), self._item_values(item))
        return item.model_copy(deep=True)

    async def get_memory(
        self, *, tenant_id: str, user_id: str, memory_id: str
    ) -> MemoryItem | None:
        return await self._fetch_one(
            "SELECT * FROM memory_items WHERE id = %s AND tenant_id = %s AND user_id = %s",
            (memory_id, tenant_id, user_id),
        )

    async def find_active_by_key(
        self, *, tenant_id: str, user_id: str, namespace: str, subject: str, key: str
    ) -> MemoryItem | None:
        return await self._fetch_one(
            """SELECT * FROM memory_items
            WHERE tenant_id = %s AND user_id = %s AND namespace = %s
              AND subject = %s AND key = %s AND status = %s
            ORDER BY updated_at DESC LIMIT 1""",
            (tenant_id, user_id, namespace, subject, key, MemoryStatus.ACTIVE),
        )

    async def search_memories(
        self, query: MemorySearchQuery, *, embedding: list[float] | None
    ) -> list[MemorySearchResult]:
        await self.initialize()
        namespace_values = list(query.namespaces)
        if embedding is None:
            semantic_expression = "0.0"
            parameters: list[Any] = [query.text]
        else:
            semantic_expression = "GREATEST(0.0, 1.0 - (embedding <=> %s))"
            parameters = [embedding, query.text]
        parameters.extend(
            [
                query.tenant_id,
                query.user_id,
                namespace_values,
                MemoryStatus.ACTIVE,
                query.text,
                query.top_k,
            ]
        )
        sql = f"""WITH ranked AS (
            SELECT *,
                LEAST(1.0, ts_rank_cd(
                    to_tsvector('simple', subject || ' ' || key || ' ' || content),
                    websearch_to_tsquery('simple', %s)
                )) AS lexical_score,
                {semantic_expression} AS semantic_score
            FROM memory_items
            WHERE tenant_id = %s AND user_id = %s
              AND namespace = ANY(%s) AND status = %s
              AND (expires_at IS NULL OR expires_at > NOW())
        )
        SELECT *, LEAST(1.0,
            lexical_score * 0.35 + semantic_score * 0.4
            + importance * 0.15 + confidence * 0.1
        ) AS combined_score
        FROM ranked
        WHERE lexical_score > 0 OR semantic_score > 0
           OR to_tsvector('simple', content) @@ websearch_to_tsquery('simple', %s)
        ORDER BY combined_score DESC LIMIT %s"""
        # The embedding expression precedes lexical_score in SQL when enabled.
        if embedding is not None:
            parameters[0], parameters[1] = parameters[1], parameters[0]
        async with self._pool.connection() as connection:
            rows = await connection.execute(sql, parameters)
            records = await rows.fetchall()
            results = [
                MemorySearchResult(
                    item=self._row_to_item(row),
                    score=float(row["combined_score"]),
                    lexical_score=float(row["lexical_score"]),
                    semantic_score=float(row["semantic_score"]),
                )
                for row in records
            ]
            if results:
                await connection.execute(
                    "UPDATE memory_items SET last_accessed_at = %s WHERE id = ANY(%s)",
                    (utc_now(), [result.item.id for result in results]),
                )
            return results

    async def list_memories(
        self,
        *,
        tenant_id: str,
        user_id: str,
        status: MemoryStatus | None = None,
        namespace: str | None = None,
        limit: int = 100,
    ) -> list[MemoryItem]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        clauses = ["tenant_id = %s", "user_id = %s"]
        parameters: list[Any] = [tenant_id, user_id]
        if status is not None:
            clauses.append("status = %s")
            parameters.append(status)
        if namespace is not None:
            clauses.append("namespace = %s")
            parameters.append(namespace)
        parameters.append(limit)
        return await self._fetch_many(
            f"""SELECT * FROM memory_items WHERE {" AND ".join(clauses)}
            ORDER BY updated_at DESC LIMIT %s""",
            parameters,
        )

    async def update_memory(self, item: MemoryItem) -> MemoryItem:
        await self.initialize()
        async with (
            self._pool.connection() as connection,
            connection.transaction(),
        ):
            if item.supersedes_id is not None and item.status == MemoryStatus.ACTIVE:
                previous = await connection.execute(
                    """UPDATE memory_items SET status = %s, updated_at = %s
                        WHERE id = %s AND tenant_id = %s AND user_id = %s""",
                    (
                        MemoryStatus.SUPERSEDED,
                        utc_now(),
                        item.supersedes_id,
                        item.tenant_id,
                        item.user_id,
                    ),
                )
                if previous.rowcount != 1:
                    raise ValueError("Superseded memory is outside the trusted owner scope")
            cursor = await connection.execute(
                """UPDATE memory_items SET
                        namespace=%s, type=%s, subject=%s, key=%s, content=%s,
                        structured_data=%s, confidence=%s, importance=%s,
                        sensitivity=%s, status=%s, supersedes_id=%s,
                        source_type=%s, source_conversation_id=%s, source_message_id=%s,
                        source_run_id=%s, source_job_id=%s, embedding=%s,
                        updated_at=%s, last_accessed_at=%s, expires_at=%s
                    WHERE id=%s AND tenant_id=%s AND user_id=%s""",
                (
                    item.namespace,
                    item.type,
                    item.subject,
                    item.key,
                    item.content,
                    json.dumps(item.structured_data),
                    item.confidence,
                    item.importance,
                    item.sensitivity,
                    item.status,
                    item.supersedes_id,
                    item.source_type,
                    item.source_conversation_id,
                    item.source_message_id,
                    item.source_run_id,
                    item.source_job_id,
                    item.embedding,
                    item.updated_at,
                    item.last_accessed_at,
                    item.expires_at,
                    item.id,
                    item.tenant_id,
                    item.user_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError("Memory not found in trusted owner scope")
        return item.model_copy(deep=True)

    async def forget_memory(self, *, tenant_id: str, user_id: str, memory_id: str) -> bool:
        await self.initialize()
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """UPDATE memory_items SET status=%s, content='[forgotten]',
                    structured_data='{}'::jsonb, embedding=NULL, updated_at=%s
                WHERE id=%s AND tenant_id=%s AND user_id=%s""",
                (MemoryStatus.DELETED, utc_now(), memory_id, tenant_id, user_id),
            )
            return bool(cursor.rowcount == 1)

    async def _fetch_one(self, sql: str, parameters: tuple[Any, ...]) -> MemoryItem | None:
        await self.initialize()
        async with self._pool.connection() as connection:
            cursor = await connection.execute(sql, parameters)
            row = await cursor.fetchone()
            return self._row_to_item(row) if row is not None else None

    async def _fetch_many(self, sql: str, parameters: list[Any]) -> list[MemoryItem]:
        await self.initialize()
        async with self._pool.connection() as connection:
            cursor = await connection.execute(sql, parameters)
            rows = await cursor.fetchall()
            return [self._row_to_item(row) for row in rows]

    @staticmethod
    def _insert_sql() -> str:
        return """INSERT INTO memory_items (
            id, tenant_id, user_id, namespace, type, subject, key, content,
            structured_data, confidence, importance, sensitivity, status,
            supersedes_id, source_type, source_conversation_id, source_message_id,
            source_run_id, source_job_id, embedding, created_at, updated_at,
            last_accessed_at, expires_at
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
        )"""

    @staticmethod
    def _item_values(item: MemoryItem) -> tuple[Any, ...]:
        return (
            item.id,
            item.tenant_id,
            item.user_id,
            item.namespace,
            item.type,
            item.subject,
            item.key,
            item.content,
            json.dumps(item.structured_data),
            item.confidence,
            item.importance,
            item.sensitivity,
            item.status,
            item.supersedes_id,
            item.source_type,
            item.source_conversation_id,
            item.source_message_id,
            item.source_run_id,
            item.source_job_id,
            item.embedding,
            item.created_at,
            item.updated_at,
            item.last_accessed_at,
            item.expires_at,
        )

    @staticmethod
    def _row_to_item(row: Mapping[str, Any]) -> MemoryItem:
        structured_data = row["structured_data"]
        if isinstance(structured_data, str):
            structured_data = json.loads(structured_data)
        embedding = row["embedding"]
        return MemoryItem(
            id=row["id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            namespace=row["namespace"],
            type=row["type"],
            subject=row["subject"],
            key=row["key"],
            content=row["content"],
            structured_data=structured_data,
            confidence=row["confidence"],
            importance=row["importance"],
            sensitivity=row["sensitivity"],
            status=row["status"],
            supersedes_id=row["supersedes_id"],
            source_type=row["source_type"],
            source_conversation_id=row["source_conversation_id"],
            source_message_id=row["source_message_id"],
            source_run_id=row["source_run_id"],
            source_job_id=row["source_job_id"],
            embedding=list(embedding) if embedding is not None else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_accessed_at=row["last_accessed_at"],
            expires_at=row["expires_at"],
        )
