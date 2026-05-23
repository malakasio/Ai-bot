"""Rollback module - re-exports from core."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
import uuid


@dataclass
class RollbackPoint:
    """Represents a rollback point."""
    id: str
    timestamp: datetime
    description: str
    git_commit: str = None


async def create_rollback_point(description: str) -> RollbackPoint:
    """Create a rollback point for the current state."""
    rp_id = f"rp_{uuid.uuid4().hex[:8]}"

    # Try to get git commit
    git_commit = None
    try:
        proc = await asyncio.create_subprocess_exec(
            'git', 'rev-parse', 'HEAD',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            git_commit = stdout.decode().strip()
    except Exception:
        pass

    return RollbackPoint(
        id=rp_id,
        timestamp=datetime.now(),
        description=description,
        git_commit=git_commit
    )


__all__ = ['create_rollback_point', 'RollbackPoint']
