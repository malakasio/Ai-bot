"""Git Worktree Isolation Manager for God Mode.

Provides isolated git worktrees for each agent, preventing conflicts
and enabling parallel execution.

Architecture:
    /root/Ai-bot/                           # Main repo
    ├── .git/
    ├── .claude/worktrees/                  # Isolated worktrees
    │   ├── task-{uuid}/                    # Per-task workspace
    │   │   ├── .git -> ../../.git/worktrees/task-{uuid}
    │   │   └── [full repo copy]

Each worktree:
- Has its own branch (godmode/task-{uuid})
- Isolated from other agents
- Can be merged back to main
- Automatically cleaned up on completion
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import UUID


@dataclass
class Worktree:
    """Represents an isolated git worktree for an agent."""

    task_id: UUID
    path: Path
    branch: str
    base_commit: str
    created_at: float


class WorktreeManager:
    """Manages git worktrees for agent isolation."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.worktrees_dir = repo_root / ".claude" / "worktrees"
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

    async def create_worktree(
        self, task_id: UUID, base_branch: str = "main", new_branch: Optional[str] = None
    ) -> Worktree:
        """
        Create an isolated git worktree for a task.

        Args:
            task_id: Unique task identifier
            base_branch: Branch to base the worktree on (default: main)
            new_branch: Name for new branch (default: godmode/task-{uuid})

        Returns:
            Worktree object with path and metadata

        Raises:
            RuntimeError: If worktree creation fails
        """
        import time

        if new_branch is None:
            new_branch = f"godmode/task-{task_id}"

        worktree_path = self.worktrees_dir / f"task-{task_id}"

        # Check if worktree already exists
        if worktree_path.exists():
            raise RuntimeError(f"Worktree already exists: {worktree_path}")

        # Get base commit SHA
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            base_branch,
            cwd=self.repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"Failed to get base commit: {stderr.decode()}")

        base_commit = stdout.decode().strip()

        # Create worktree with new branch
        proc = await asyncio.create_subprocess_exec(
            "git",
            "worktree",
            "add",
            "-b",
            new_branch,
            str(worktree_path),
            base_branch,
            cwd=self.repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"Failed to create worktree: {stderr.decode()}")

        print(f"[worktree] Created: {worktree_path} (branch: {new_branch})")

        return Worktree(
            task_id=task_id,
            path=worktree_path,
            branch=new_branch,
            base_commit=base_commit,
            created_at=time.time(),
        )

    async def list_worktrees(self) -> list[dict]:
        """List all git worktrees."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            "worktree",
            "list",
            "--porcelain",
            cwd=self.repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"Failed to list worktrees: {stderr.decode()}")

        # Parse porcelain output
        worktrees = []
        current = {}

        for line in stdout.decode().split("\n"):
            line = line.strip()
            if not line:
                if current:
                    worktrees.append(current)
                    current = {}
                continue

            if line.startswith("worktree "):
                current["path"] = line.split(" ", 1)[1]
            elif line.startswith("HEAD "):
                current["commit"] = line.split(" ", 1)[1]
            elif line.startswith("branch "):
                current["branch"] = line.split(" ", 1)[1]
            elif line == "bare":
                current["bare"] = True
            elif line == "detached":
                current["detached"] = True

        if current:
            worktrees.append(current)

        return worktrees

    async def get_changes(self, worktree: Worktree) -> dict:
        """
        Get summary of changes in a worktree.

        Returns:
            Dict with: files_changed, insertions, deletions, commits
        """
        # Get diff stats
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--stat",
            worktree.base_commit,
            "HEAD",
            cwd=worktree.path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            return {"error": stderr.decode()}

        # Get list of changed files
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--name-only",
            worktree.base_commit,
            "HEAD",
            cwd=worktree.path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        files_stdout, _ = await proc.communicate()

        files_changed = [f for f in files_stdout.decode().split("\n") if f]

        # Get commit count
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-list",
            "--count",
            f"{worktree.base_commit}..HEAD",
            cwd=worktree.path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        count_stdout, _ = await proc.communicate()

        commit_count = int(count_stdout.decode().strip()) if count_stdout else 0

        # Get commit SHAs
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-list",
            f"{worktree.base_commit}..HEAD",
            cwd=worktree.path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        commits_stdout, _ = await proc.communicate()

        commits = [c for c in commits_stdout.decode().split("\n") if c]

        return {
            "files_changed": files_changed,
            "file_count": len(files_changed),
            "commit_count": commit_count,
            "commits": commits,
            "diff_stat": stdout.decode(),
        }

    async def merge_worktree(
        self, worktree: Worktree, target_branch: str = "main", squash: bool = False
    ) -> dict:
        """
        Merge worktree changes back to target branch.

        Args:
            worktree: Worktree to merge
            target_branch: Branch to merge into (default: main)
            squash: Whether to squash commits (default: False)

        Returns:
            Dict with: success, merge_commit, conflicts
        """
        # Switch to target branch in main repo
        proc = await asyncio.create_subprocess_exec(
            "git",
            "checkout",
            target_branch,
            cwd=self.repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        if proc.returncode != 0:
            return {"success": False, "error": "Failed to checkout target branch"}

        # Merge worktree branch
        merge_args = ["git", "merge"]
        if squash:
            merge_args.append("--squash")
        merge_args.extend(
            ["--no-ff", "-m", f"Merge God Mode task {worktree.task_id}", worktree.branch]
        )

        proc = await asyncio.create_subprocess_exec(
            *merge_args,
            cwd=self.repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            # Check for conflicts
            if b"CONFLICT" in stderr or b"CONFLICT" in stdout:
                return {
                    "success": False,
                    "conflicts": True,
                    "error": "Merge conflicts detected",
                    "output": stdout.decode() + stderr.decode(),
                }
            return {"success": False, "conflicts": False, "error": stderr.decode()}

        # Get merge commit SHA
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=self.repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        merge_commit = stdout.decode().strip()

        print(f"[worktree] Merged {worktree.branch} -> {target_branch} ({merge_commit[:8]})")

        return {"success": True, "merge_commit": merge_commit, "conflicts": False}

    async def cleanup_worktree(self, worktree: Worktree, force: bool = False) -> bool:
        """
        Remove a worktree and its branch.

        Args:
            worktree: Worktree to remove
            force: Force removal even with uncommitted changes

        Returns:
            True if successful, False otherwise
        """
        # Remove worktree
        remove_args = ["git", "worktree", "remove"]
        if force:
            remove_args.append("--force")
        remove_args.append(str(worktree.path))

        proc = await asyncio.create_subprocess_exec(
            *remove_args,
            cwd=self.repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            print(f"[worktree] Failed to remove worktree: {stderr.decode()}")
            return False

        # Delete branch
        proc = await asyncio.create_subprocess_exec(
            "git",
            "branch",
            "-D",
            worktree.branch,
            cwd=self.repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        print(f"[worktree] Cleaned up: {worktree.path}")
        return True

    async def has_uncommitted_changes(self, worktree: Worktree) -> bool:
        """Check if worktree has uncommitted changes."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            cwd=worktree.path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        return bool(stdout.decode().strip())
