"""Memory API — episodic, semantic, procedural, task queue.

Thin async functions over core.database. No business logic beyond
serialization, parameter binding, and a sensible default ordering.

The three functions named in the goal are surfaced at module top:
    store_episode()
    semantic_search()
    get_task_queue()

Everything else is built around them.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from uuid import UUID

from . import database


# ─── Types ────────────────────────────────────────────────────────────────


@dataclass
class Episode:
    actor: str
    tool: str
    input: Optional[str] = None
    output: Optional[str] = None
    exit_code: Optional[int] = None
    zone: Optional[str] = None
    score: Optional[int] = None
    duration_ms: Optional[int] = None
    failure_mode: Optional[str] = None
    task_id: Optional[UUID] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SemanticMatch:
    id: UUID
    kind: str
    subject: str
    content: str
    confidence: float
    observation_count: int
    distance: float
    metadata: dict[str, Any]


@dataclass
class QueuedTask:
    id: UUID
    kind: str
    target: str
    payload: dict[str, Any]
    priority: int
    attempts: int
    max_attempts: int
    scheduled_for: datetime
    created_at: datetime


# ─── 2. Episodic ──────────────────────────────────────────────────────────


async def store_episode(ep: Episode) -> int:
    """Insert one episode into history_logs. Returns the new row id."""
    row = await database.fetchrow(
        """
        INSERT INTO history_logs (
            actor, tool, input, output, exit_code, zone, score,
            duration_ms, failure_mode, task_id, metadata
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb
        )
        RETURNING id
        """,
        ep.actor,
        ep.tool,
        ep.input,
        ep.output,
        ep.exit_code,
        ep.zone,
        ep.score,
        ep.duration_ms,
        ep.failure_mode,
        ep.task_id,
        json.dumps(ep.metadata),
    )
    return int(row["id"])


async def recent_episodes(
    limit: int = 100,
    actor: Optional[str] = None,
    since: Optional[datetime] = None,
) -> list[dict]:
    """Most-recent-first read from history_logs."""
    clauses = []
    args: list[Any] = []
    if actor is not None:
        args.append(actor)
        clauses.append(f"actor = ${len(args)}")
    if since is not None:
        args.append(since)
        clauses.append(f"ts >= ${len(args)}")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(limit)
    rows = await database.fetch(
        f"""
        SELECT id, ts, actor, tool, input, output, exit_code, zone,
               score, duration_ms, failure_mode, task_id, metadata
        FROM history_logs
        {where}
        ORDER BY ts DESC
        LIMIT ${len(args)}
        """,
        *args,
    )
    return [dict(r) for r in rows]


# ─── 3. Semantic ──────────────────────────────────────────────────────────


def _vector_literal(values: Sequence[float]) -> str:
    """Serialize a vector as pgvector's text literal, when the codec is absent."""
    return "[" + ",".join(f"{float(v):.7g}" for v in values) + "]"


async def upsert_semantic(
    kind: str,
    subject: str,
    content: str,
    embedding: Sequence[float],
    confidence: float = 0.5,
    metadata: Optional[dict[str, Any]] = None,
    source_episode: Optional[int] = None,
) -> UUID:
    """Insert a new semantic-memory row. Returns its id.

    Conflict resolution / merging (autoDream) is a separate routine; this
    is intentionally a plain insert.
    """
    row = await database.fetchrow(
        """
        INSERT INTO jarvis_semantic_memory (
            kind, subject, content, confidence, embedding, metadata,
            source_episode
        ) VALUES (
            $1, $2, $3, $4, $5::vector, $6::jsonb, $7
        )
        RETURNING id
        """,
        kind,
        subject,
        content,
        confidence,
        _vector_literal(embedding),
        json.dumps(metadata or {}),
        source_episode,
    )
    return row["id"]


async def semantic_search(
    embedding: Sequence[float],
    limit: int = 10,
    kind: Optional[str] = None,
    min_confidence: float = 0.0,
) -> list[SemanticMatch]:
    """Top-K nearest neighbors by cosine distance.

    `embedding` is a sequence of floats with the dimension of the column
    (1536 by default in the schema). Lower `distance` = more similar.
    """
    clauses = ["confidence >= $2"]
    args: list[Any] = [_vector_literal(embedding), float(min_confidence)]
    if kind is not None:
        args.append(kind)
        clauses.append(f"kind = ${len(args)}")
    args.append(int(limit))
    rows = await database.fetch(
        f"""
        SELECT id, kind, subject, content, confidence, observation_count,
               metadata,
               embedding <=> $1::vector AS distance
        FROM jarvis_semantic_memory
        WHERE {" AND ".join(clauses)}
        ORDER BY embedding <=> $1::vector
        LIMIT ${len(args)}
        """,
        *args,
    )
    out: list[SemanticMatch] = []
    for r in rows:
        meta = r["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        out.append(
            SemanticMatch(
                id=r["id"],
                kind=r["kind"],
                subject=r["subject"],
                content=r["content"],
                confidence=float(r["confidence"]),
                observation_count=int(r["observation_count"]),
                distance=float(r["distance"]),
                metadata=meta or {},
            )
        )
    return out


# ─── 4. Procedural ────────────────────────────────────────────────────────


async def upsert_skill(
    name: str,
    description: str = "",
    version: str = "1.0",
    skill_md_path: Optional[str] = None,
    inputs_schema: Optional[dict] = None,
    outputs_schema: Optional[dict] = None,
) -> UUID:
    row = await database.fetchrow(
        """
        INSERT INTO skills (name, version, description, skill_md_path,
                            inputs_schema, outputs_schema)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
        ON CONFLICT (name) DO UPDATE SET
          version = EXCLUDED.version,
          description = EXCLUDED.description,
          skill_md_path = EXCLUDED.skill_md_path,
          inputs_schema = EXCLUDED.inputs_schema,
          outputs_schema = EXCLUDED.outputs_schema,
          updated_at = now()
        RETURNING id
        """,
        name,
        version,
        description,
        skill_md_path,
        json.dumps(inputs_schema or {}),
        json.dumps(outputs_schema or {}),
    )
    return row["id"]


async def record_skill_outcome(
    name: str, success: bool, score: Optional[int] = None,
    failure_mode: Optional[str] = None, lesson: Optional[str] = None,
) -> None:
    """Update skill counters and (optionally) append a lesson."""
    async with await database.transaction() as conn:
        skill = await conn.fetchrow(
            "SELECT id FROM skills WHERE name = $1", name
        )
        if not skill:
            return
        if success:
            await conn.execute(
                "UPDATE skills SET success_count = success_count + 1, "
                "last_run_at = now() WHERE id = $1",
                skill["id"],
            )
        else:
            await conn.execute(
                "UPDATE skills SET failure_count = failure_count + 1, "
                "last_run_at = now() WHERE id = $1",
                skill["id"],
            )
        if lesson is not None or score is not None:
            await conn.execute(
                """
                INSERT INTO skill_lessons (skill_id, score, failure_mode, lesson)
                VALUES ($1, $2, $3, $4)
                """,
                skill["id"], score, failure_mode, lesson or "",
            )


# ─── Task queue (KAIROS) ──────────────────────────────────────────────────


_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


async def enqueue_task(
    kind: str,
    target: str,
    payload: Optional[dict] = None,
    priority: int = 100,
    scheduled_for: Optional[datetime] = None,
    notify: bool = False,
    max_attempts: int = 3,
) -> UUID:
    row = await database.fetchrow(
        """
        INSERT INTO task_queue (kind, target, payload, priority,
                                scheduled_for, notify, max_attempts)
        VALUES ($1, $2, $3::jsonb, $4, COALESCE($5, now()), $6, $7)
        RETURNING id
        """,
        kind, target, json.dumps(payload or {}), priority,
        scheduled_for, notify, max_attempts,
    )
    return row["id"]


async def get_task_queue(limit: int = 20) -> list[QueuedTask]:
    """Peek at the next-due queued tasks. Read-only.

    For an atomic checkout (claim) use `claim_next_task()`.
    """
    rows = await database.fetch(
        """
        SELECT id, kind, target, payload, priority, attempts,
               max_attempts, scheduled_for, created_at
        FROM task_queue
        WHERE status = 'queued' AND scheduled_for <= now()
        ORDER BY priority ASC, scheduled_for ASC
        LIMIT $1
        """,
        int(limit),
    )
    out = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        out.append(
            QueuedTask(
                id=r["id"],
                kind=r["kind"],
                target=r["target"],
                payload=payload or {},
                priority=int(r["priority"]),
                attempts=int(r["attempts"]),
                max_attempts=int(r["max_attempts"]),
                scheduled_for=r["scheduled_for"],
                created_at=r["created_at"],
            )
        )
    return out


async def claim_next_task(worker_id: Optional[str] = None) -> Optional[QueuedTask]:
    """Atomically claim the highest-priority due task.

    Uses SELECT … FOR UPDATE SKIP LOCKED so multiple KAIROS workers can
    drain the queue concurrently without stepping on each other.
    """
    worker = worker_id or _WORKER_ID
    async with await database.transaction() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, kind, target, payload, priority, attempts,
                   max_attempts, scheduled_for, created_at
            FROM task_queue
            WHERE status = 'queued' AND scheduled_for <= now()
            ORDER BY priority ASC, scheduled_for ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )
        if not row:
            return None
        await conn.execute(
            """
            UPDATE task_queue
            SET status = 'running',
                locked_by = $2,
                locked_at = now(),
                started_at = COALESCE(started_at, now()),
                attempts = attempts + 1
            WHERE id = $1
            """,
            row["id"], worker,
        )
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return QueuedTask(
            id=row["id"],
            kind=row["kind"],
            target=row["target"],
            payload=payload or {},
            priority=int(row["priority"]),
            attempts=int(row["attempts"]) + 1,
            max_attempts=int(row["max_attempts"]),
            scheduled_for=row["scheduled_for"],
            created_at=row["created_at"],
        )


async def complete_task(
    task_id: UUID, result: Optional[dict] = None, error: Optional[str] = None,
) -> None:
    """Mark a claimed task done or failed."""
    if error is None:
        await database.execute(
            """
            UPDATE task_queue
            SET status = 'done', result = $2::jsonb, finished_at = now(),
                locked_by = NULL, locked_at = NULL
            WHERE id = $1
            """,
            task_id, json.dumps(result or {}),
        )
    else:
        # Failed but retryable? Re-queue if attempts < max_attempts.
        await database.execute(
            """
            UPDATE task_queue
            SET status = CASE
                  WHEN attempts >= max_attempts THEN 'failed'::task_status
                  ELSE 'queued'::task_status
                END,
                error = $2,
                finished_at = CASE
                  WHEN attempts >= max_attempts THEN now() ELSE NULL
                END,
                locked_by = NULL,
                locked_at = NULL
            WHERE id = $1
            """,
            task_id, error,
        )
