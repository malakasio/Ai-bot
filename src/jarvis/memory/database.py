"""
Unified database layer with adapter pattern for SQLite and PostgreSQL.

Detects backend from DATABASE_URL environment variable:
- If DATABASE_URL starts with 'postgresql://' → PostgreSQL adapter
- Otherwise → SQLite adapter (default)

All v6 fixes applied to SQLite:
- Single writer via asyncio.Queue + Future
- WAL mode + required PRAGMAs
- Supervised writer task with drain-on-crash
- Hourly WAL checkpoint
"""
from __future__ import annotations

import os
from typing import Any

from jarvis.observability.logger import get_logger

log = get_logger("db")

# Global adapter instance
_adapter = None


def _detect_backend() -> str:
    """Detect database backend from environment."""
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url.startswith("postgresql://") or database_url.startswith("postgres://"):
        return "postgres"
    return "sqlite"


async def init() -> None:
    """Initialize the database adapter based on detected backend."""
    global _adapter

    if _adapter is not None:
        return  # Already initialized

    backend = _detect_backend()
    log.info(f"Initializing {backend} database adapter")

    if backend == "postgres":
        from jarvis.memory.adapters.postgres import PostgreSQLAdapter
        _adapter = PostgreSQLAdapter()
    else:
        from jarvis.memory.adapters.sqlite import SQLiteAdapter
        _adapter = SQLiteAdapter()

    await _adapter.init()


async def close() -> None:
    """Close the database connection/pool."""
    global _adapter
    if _adapter is not None:
        await _adapter.close()
        _adapter = None


def _get_adapter():
    """Get the initialized adapter or raise."""
    if _adapter is None:
        raise RuntimeError("Database not initialized. Call await init() first.")
    return _adapter


# ─── Convenience API (matches both SQLite and PostgreSQL interfaces) ──────


async def execute(query: str, *args: Any) -> Any:
    """Execute a write query."""
    return await _get_adapter().execute(query, *args)


async def fetch(query: str, *args: Any) -> list[dict[str, Any]]:
    """Fetch all rows."""
    return await _get_adapter().fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
    """Fetch a single row."""
    return await _get_adapter().fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    """Fetch a single value."""
    return await _get_adapter().fetchval(query, *args)


# ─── Legacy compatibility (for existing src/jarvis code) ──────────────────


async def db_write(sql: str, params: tuple = ()) -> int:
    """Execute a write. Returns lastrowid (SQLite) or 0 (PostgreSQL)."""
    result = await execute(sql, *params)
    return result if isinstance(result, int) else 0


async def db_fetch_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    """Fetch a single row."""
    return await fetchrow(sql, *params)


async def db_fetch_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Fetch all rows."""
    return await fetch(sql, *params)


async def shutdown_db():
    """Shutdown the database (alias for close)."""
    await close()
