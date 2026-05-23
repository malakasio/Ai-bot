"""Database adapters for SQLite and PostgreSQL backends."""

from __future__ import annotations

from typing import Protocol, Any


class DatabaseAdapter(Protocol):
    """Protocol for database backend adapters."""

    async def init(self) -> None:
        """Initialize the database connection/pool."""
        ...

    async def close(self) -> None:
        """Close the database connection/pool."""
        ...

    async def execute(self, query: str, *args: Any) -> Any:
        """Execute a write query."""
        ...

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        """Fetch all rows."""
        ...

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        """Fetch a single row."""
        ...

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Fetch a single value."""
        ...


__all__ = ["DatabaseAdapter"]
