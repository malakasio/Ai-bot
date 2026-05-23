"""SQLite adapter with single-writer pattern (extracted from database.py)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from jarvis.config import get_config
from jarvis.observability.logger import get_logger

log = get_logger("db.sqlite")


@dataclass
class DBRequest:
    sql: str
    params: tuple
    future: asyncio.Future
    fetch_mode: str = "none"  # "none" | "one" | "all" | "rowcount"


# Global single-writer queue
db_queue: asyncio.Queue[DBRequest | None] = asyncio.Queue()
_db_ready = asyncio.Event()


INIT_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=10000;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-65536;
PRAGMA foreign_keys=ON;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    content     TEXT    NOT NULL,
    embedding   BLOB,
    importance  REAL    DEFAULT 0.5,
    memory_type TEXT    NOT NULL CHECK(memory_type IN ('episodic','semantic')),
    tags        TEXT    DEFAULT '[]',
    session_id  TEXT,
    source      TEXT,
    confidence  REAL    DEFAULT 0.8,
    consolidated INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    role        TEXT    NOT NULL CHECK(role IN ('user','assistant','system','tool')),
    content     TEXT    NOT NULL,
    session_id  TEXT    NOT NULL,
    tool_name   TEXT,
    compressed  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT    PRIMARY KEY,
    created_at  REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    priority    INTEGER DEFAULT 3,
    task_type   TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    status      TEXT    DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed','cancelled')),
    result      TEXT,
    error       TEXT,
    score       REAL,
    started_at  REAL,
    finished_at REAL,
    agent_id    TEXT
);

CREATE TABLE IF NOT EXISTS api_costs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    model       TEXT    NOT NULL,
    input_tok   INTEGER DEFAULT 0,
    output_tok  INTEGER DEFAULT 0,
    cost_usd    REAL    DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS checkpoints (
    task_id     TEXT    PRIMARY KEY,
    ts          REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    state_json  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    skill_name  TEXT    NOT NULL,
    proposal    TEXT    NOT NULL,
    status      TEXT    DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected'))
);

CREATE TABLE IF NOT EXISTS action_log (
    action_id   TEXT    PRIMARY KEY,
    ts          REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    tool        TEXT    NOT NULL,
    input       TEXT,
    output      TEXT,
    success     INTEGER DEFAULT 1,
    duration_ms REAL,
    score       REAL,
    model_used  TEXT,
    tokens_used INTEGER,
    affected    TEXT    DEFAULT '[]',
    zone        TEXT    DEFAULT 'green'
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
    USING fts5(content, tokenize='unicode61');

CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_consolidated ON memories(consolidated);
CREATE INDEX IF NOT EXISTS idx_sessions_session ON sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_compressed ON sessions(compressed);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, priority);
CREATE INDEX IF NOT EXISTS idx_api_costs_ts ON api_costs(ts);
"""


async def db_writer_task():
    """Single writer coroutine — owns the DB connection exclusively."""
    db_path = Path(get_config().memory.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(INIT_SQL)
        await db.commit()
        _db_ready.set()
        log.info(f"SQLite database ready at {db_path}")

        last_checkpoint = time.time()

        while True:
            try:
                req = await asyncio.wait_for(db_queue.get(), timeout=60.0)
            except asyncio.TimeoutError:
                # WAL checkpoint
                interval = get_config().memory.wal_checkpoint_interval_s
                if time.time() - last_checkpoint > interval:
                    try:
                        await db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                        last_checkpoint = time.time()
                        log.debug("WAL checkpoint completed")
                    except Exception as e:
                        log.warning(f"WAL checkpoint failed: {e}")
                continue

            if req is None:
                log.info("SQLite writer shutting down")
                break

            try:
                if req.fetch_mode == "all":
                    cursor = await db.execute(req.sql, req.params)
                    rows = await cursor.fetchall()
                    req.future.set_result([dict(r) for r in rows])
                elif req.fetch_mode == "one":
                    cursor = await db.execute(req.sql, req.params)
                    row = await cursor.fetchone()
                    req.future.set_result(dict(row) if row else None)
                elif req.fetch_mode == "rowcount":
                    cursor = await db.execute(req.sql, req.params)
                    await db.commit()
                    req.future.set_result(cursor.rowcount)
                else:
                    cursor = await db.execute(req.sql, req.params)
                    await db.commit()
                    req.future.set_result(cursor.lastrowid)
            except Exception as e:
                req.future.set_exception(e)
            finally:
                db_queue.task_done()


async def supervised_db_writer():
    """Restarts db_writer_task on crash."""
    while True:
        try:
            await db_writer_task()
            return
        except Exception as e:
            log.critical(f"SQLite writer crashed: {e}", exc_info=True)
            _db_ready.clear()
            drained = 0
            while True:
                try:
                    req = db_queue.get_nowait()
                    if req and not req.future.done():
                        req.future.set_exception(RuntimeError("DB writer restarted"))
                    drained += 1
                except asyncio.QueueEmpty:
                    break
            if drained:
                log.warning(f"Drained {drained} pending DB requests after crash")
            await asyncio.sleep(2)


class SQLiteAdapter:
    """SQLite database adapter with single-writer pattern."""

    def __init__(self):
        self._writer_task = None

    async def init(self) -> None:
        """Start the supervised writer task."""
        if self._writer_task is None:
            self._writer_task = asyncio.create_task(supervised_db_writer())
        await _db_ready.wait()

    async def close(self) -> None:
        """Send poison pill to writer task."""
        await db_queue.put(None)
        if self._writer_task:
            await self._writer_task

    async def execute(self, query: str, *args: Any) -> int:
        """Execute a write. Returns lastrowid."""
        await _db_ready.wait()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await db_queue.put(DBRequest(query, args, future, "none"))
        return await future

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        """Fetch all rows."""
        await _db_ready.wait()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await db_queue.put(DBRequest(query, args, future, "all"))
        return await future

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        """Fetch a single row."""
        await _db_ready.wait()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await db_queue.put(DBRequest(query, args, future, "one"))
        return await future

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Fetch a single value."""
        row = await self.fetchrow(query, *args)
        return list(row.values())[0] if row else None
