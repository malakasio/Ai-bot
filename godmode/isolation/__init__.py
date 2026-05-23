"""God Mode Isolation Module.

Provides isolation mechanisms for parallel agent execution:
- Git worktree isolation (worktree.py)
- Docker container sandboxing (docker.py)
"""

from .worktree import Worktree, WorktreeManager
from .docker import Container, DockerManager

__all__ = ["Worktree", "WorktreeManager", "Container", "DockerManager"]
