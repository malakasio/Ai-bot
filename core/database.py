"""Async PostgreSQL connection pool.

Built on asyncpg. One pool per process, lazy-initialized. Callers acquire
connections through `pool()` or run one-off queries through `fetch()` /
`fetchrow()` / `execute()`.

Configuration via environment (or DATABASE_URL):
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
    DATABASE_URL          # if set, overrides the above
    DB_POOL_MIN_SIZE      # default 1
    DB_POOL_MAX_SIZE      # default 10
    DB_COMMAND_TIMEOUT    # default 30 (seconds)

asyncpg is registered with the pgvector adapter automatically if the
`pgvector` package is installed; otherwise vectors round-trip as text and
the caller is responsible for serialization.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

# ─── Load .env at import time ─────────────────────────────────────────────
try:
    from dotenv import load_dotenv as _load_dotenv

    _ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_FILE.is_file():
        _load_dotenv(_ENV_FILE, override=False)
    else:
        _load_dotenv(override=False)
except Exception:
    pass

try:
    import asyncpg
except ImportError as _e:  # pragma: no cover - dependency surface
    asyncpg = None  # type: ignore[assignment]
    _ASYNCPG_IMPORT_ERROR: Optional[Exception] = _e
else:
    _ASYNCPG_IMPORT_ERROR = None


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
        # pgvector not installed, or extension not loaded in this DB.
        # Vectors will round-trip as text; callers using semantic_search()
        # must serialize accordingly.
        pass


async def init() -> "asyncpg.Pool":
    """Initialize (or return the existing) pool. Safe to call repeatedly."""
    if asyncpg is None:
        raise RuntimeError(
            f"asyncpg is not installed; pip install asyncpg pgvector ({_ASYNCPG_IMPORT_ERROR})"
        )
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
    return _pool


async def pool() -> "asyncpg.Pool":
    """Return the live pool, initializing on first call."""
    return _pool or await init()


async def close() -> None:
    """Close the pool. Called on shutdown."""
    global _pool
    async with _pool_lock:
        if _pool is not None:
            await _pool.close()
            _pool = None


# ─── Convenience one-shot helpers ─────────────────────────────────────────


async def execute(query: str, *args: Any) -> str:
    p = await pool()
    async with p.acquire() as conn:
        return await conn.execute(query, *args)


async def fetch(query: str, *args: Any) -> list:
    p = await pool()
    async with p.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args: Any):
    p = await pool()
    async with p.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    p = await pool()
    async with p.acquire() as conn:
        return await conn.fetchval(query, *args)


def transaction():
    """Async context manager yielding a connection in a transaction.

    Usage:
        async with database.transaction() as conn:
            await conn.execute(...)
            await conn.execute(...)
    """
    return _TransactionCtx()


class _TransactionCtx:
    def __init__(self) -> None:
        self._conn: Optional["asyncpg.Connection"] = None
        self._txn = None
        self._pool: Optional["asyncpg.Pool"] = None

    async def __aenter__(self):
        self._pool = await pool()
        self._conn = await self._pool.acquire()
        self._txn = self._conn.transaction()
        await self._txn.start()
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                await self._txn.commit()
            else:
                await self._txn.rollback()
        except Exception as e:
            # Ensure transaction is closed even on error
            try:
                await self._txn.rollback()
            except:
                pass
            raise e
        finally:
            if self._conn is not None and self._pool is not None:
                await self._pool.release(self._conn)


# Synchronous schema bootstrap for test/setup contexts.
def init_schema_sync(sql_path: str | os.PathLike[str] | None = None) -> None:
    """Run scripts/init_db.sql via psql. Synchronous; intended for setup."""
    import subprocess
    from pathlib import Path

    here = Path(__file__).resolve().parent.parent
    sql = Path(sql_path) if sql_path else here / "scripts" / "init_db.sql"
    if not sql.exists():
        raise FileNotFoundError(sql)

    cmd = ["psql"]
    url = os.environ.get("DATABASE_URL")
    if url:
        cmd.extend(["-d", url])
    else:
        cmd.extend(
            [
                "-h",
                os.environ.get("POSTGRES_HOST", "localhost"),
                "-p",
                os.environ.get("POSTGRES_PORT", "5432"),
                "-U",
                os.environ.get("POSTGRES_USER", "jarvis"),
                "-d",
                os.environ.get("POSTGRES_DB", "jarvis"),
            ]
        )
    cmd.extend(["-v", "ON_ERROR_STOP=1", "-f", str(sql)])
    env = os.environ.copy()
    if "POSTGRES_PASSWORD" in env:
        env["PGPASSWORD"] = env["POSTGRES_PASSWORD"]
    subprocess.run(cmd, env=env, check=True)
