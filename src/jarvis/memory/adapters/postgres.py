"""PostgreSQL adapter with connection pooling (from core/database.py)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

from jarvis.observability.logger import get_logger

log = get_logger("db.postgres")

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv as _load_dotenv
    _ENV_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".env"
    if _ENV_FILE.is_file():
        _load_dotenv(_ENV_FILE, override=False)
    else:
        _load_dotenv(override=False)
except Exception:
    pass


_pool_lock = asyncio.Lock()
_pool: Optional["asyncpg.Pool"] = None


def _dsn() -> str:
    """Build a libpq-style DSN from env, or use DATABASE_URL if present."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "jarvis")
    user = os.environ.get("POSTGRES_USER", "jarvis")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    auth = f"{user}:{password}@" if password else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{db}"


async def _register_vector(conn: "asyncpg.Connection") -> None:
    """Register the pgvector type codec on each new connection if available."""
    try:
        from pgvector.asyncpg import register_vector  # type: ignore
        await register_vector(conn)
    except Exception:
        pass


class PostgreSQLAdapter:
    """PostgreSQL database adapter with connection pooling."""

    def __init__(self):
        self._pool: Optional["asyncpg.Pool"] = None

    async def init(self) -> None:
        """Initialize the connection pool."""
        if asyncpg is None:
            raise RuntimeError("asyncpg is not installed; pip install asyncpg pgvector")

        global _pool
        async with _pool_lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(
                    dsn=_dsn(),
                    min_size=int(os.environ.get("DB_POOL_MIN_SIZE", "1")),
                    max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "10")),
                    command_timeout=float(os.environ.get("DB_COMMAND_TIMEOUT", "30")),
                    init=_register_vector,
                )
                self._pool = _pool
                log.info(f"PostgreSQL pool ready: {_dsn().split('@')[-1]}")

    async def close(self) -> None:
        """Close the connection pool."""
        global _pool
        async with _pool_lock:
            if _pool is not None:
                await _pool.close()
                _pool = None
                self._pool = None
                log.info("PostgreSQL pool closed")

    async def _get_pool(self) -> "asyncpg.Pool":
        """Get the pool, initializing if needed."""
        if self._pool is None:
            await self.init()
        return self._pool

    async def execute(self, query: str, *args: Any) -> str:
        """Execute a write query. Returns status string."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        """Fetch all rows."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return [dict(row) for row in rows]

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        """Fetch a single row."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, *args)
            return dict(row) if row else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Fetch a single value."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(query, *args)
