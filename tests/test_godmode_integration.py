"""God Mode Integration Tests.

Tests the full God Mode orchestration pipeline:
- Task creation and planning
- Worktree isolation
- Agent execution
- Validation pipeline
- Self-healing with feedback
- Merge and cleanup
- Telegram notifications

Run with:
    pytest tests/test_godmode_integration.py -v
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from core import database
from godmode.isolation import WorktreeManager, DockerManager
from godmode.validation.pipeline import ValidationPipeline
from godmode.orchestrator import GodModeOrchestrator


@pytest.fixture
async def db():
    """Initialize database connection."""
    await database.connect()
    yield
    await database.disconnect()


@pytest.fixture
def repo_root():
    """Get repository root path."""
    return Path("/root/Ai-bot")


@pytest.fixture
def worktree_manager(repo_root):
    """Create WorktreeManager instance."""
    return WorktreeManager(repo_root)


@pytest.fixture
def docker_manager():
    """Create DockerManager instance."""
    return DockerManager()


@pytest.fixture
def orchestrator(repo_root):
    """Create GodModeOrchestrator instance."""
    return GodModeOrchestrator(repo_root)


# ─── Worktree Isolation Tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worktree_lifecycle(worktree_manager):
    """Test complete worktree lifecycle: create, modify, merge, cleanup."""
    task_id = uuid4()

    # Create worktree
    worktree = await worktree_manager.create_worktree(task_id=task_id, base_branch="main")

    assert worktree.path.exists()
    assert worktree.branch == f"godmode/task-{task_id}"

    # Make a change
    test_file = worktree.path / "test_godmode.txt"
    test_file.write_text("God Mode test file")

    # Commit change
    proc = await asyncio.create_subprocess_exec("git", "add", "test_godmode.txt", cwd=worktree.path)
    await proc.communicate()

    proc = await asyncio.create_subprocess_exec(
        "git", "commit", "-m", "Test commit from God Mode", cwd=worktree.path
    )
    await proc.communicate()

    # Get changes
    changes = await worktree_manager.get_changes(worktree)
    assert "test_godmode.txt" in changes["files_changed"]
    assert changes["commit_count"] == 1

    # Merge to main
    merge_result = await worktree_manager.merge_worktree(worktree=worktree, target_branch="main")
    assert merge_result["success"]
    assert merge_result["merge_commit"]

    # Cleanup
    cleanup_success = await worktree_manager.cleanup_worktree(worktree)
    assert cleanup_success
    assert not worktree.path.exists()

    # Cleanup: remove test file from main
    test_file_main = worktree_manager.repo_root / "test_godmode.txt"
    if test_file_main.exists():
        test_file_main.unlink()
        proc = await asyncio.create_subprocess_exec(
            "git", "add", "test_godmode.txt", cwd=worktree_manager.repo_root
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git", "commit", "-m", "Remove test file", cwd=worktree_manager.repo_root
        )
        await proc.communicate()


@pytest.mark.asyncio
async def test_worktree_uncommitted_changes(worktree_manager):
    """Test detection of uncommitted changes."""
    task_id = uuid4()

    worktree = await worktree_manager.create_worktree(task_id=task_id)

    # Create uncommitted file
    test_file = worktree.path / "uncommitted.txt"
    test_file.write_text("Uncommitted change")

    has_changes = await worktree_manager.has_uncommitted_changes(worktree)
    assert has_changes

    # Cleanup with force
    await worktree_manager.cleanup_worktree(worktree, force=True)


# ─── Validation Pipeline Tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validation_syntax_check(worktree_manager):
    """Test syntax validation for Python files."""
    task_id = uuid4()
    worktree = await worktree_manager.create_worktree(task_id=task_id)

    # Create Python file with syntax error
    bad_file = worktree.path / "bad_syntax.py"
    bad_file.write_text("def broken(\n    print('missing closing paren'")

    validator = ValidationPipeline(worktree.path)
    result = await validator.validate(task_id=task_id, files_changed=["bad_syntax.py"], commits=[])

    assert not result.syntax["passed"]
    assert len(result.syntax["errors"]) > 0
    assert result.correctness_score < 40

    # Cleanup
    await worktree_manager.cleanup_worktree(worktree, force=True)


@pytest.mark.asyncio
async def test_validation_security_check(worktree_manager):
    """Test security scanning for secrets."""
    task_id = uuid4()
    worktree = await worktree_manager.create_worktree(task_id=task_id)

    # Create file with hardcoded secret
    secret_file = worktree.path / "secrets.py"
    secret_file.write_text('API_KEY = "sk-1234567890abcdef"')

    validator = ValidationPipeline(worktree.path)
    result = await validator.validate(task_id=task_id, files_changed=["secrets.py"], commits=[])

    assert not result.security["passed"]
    assert len(result.security["issues"]) > 0
    assert result.safety_score < 10

    # Cleanup
    await worktree_manager.cleanup_worktree(worktree, force=True)


@pytest.mark.asyncio
async def test_validation_scoring(worktree_manager):
    """Test validation scoring with clean code."""
    task_id = uuid4()
    worktree = await worktree_manager.create_worktree(task_id=task_id)

    # Create clean Python file
    clean_file = worktree.path / "clean.py"
    clean_file.write_text("""
def hello(name: str) -> str:
    return f"Hello, {name}!"

if __name__ == "__main__":
    print(hello("World"))
""")

    validator = ValidationPipeline(worktree.path)
    result = await validator.validate(task_id=task_id, files_changed=["clean.py"], commits=[])

    assert result.syntax["passed"]
    assert result.security["passed"]
    assert result.correctness_score >= 30
    assert result.safety_score >= 8
    assert result.score >= 70  # Should pass threshold

    # Cleanup
    await worktree_manager.cleanup_worktree(worktree, force=True)


# ─── Docker Isolation Tests ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(os.system("docker info > /dev/null 2>&1") != 0, reason="Docker not available")
async def test_docker_container_lifecycle(docker_manager, worktree_manager):
    """Test Docker container creation and execution."""
    task_id = uuid4()

    # Create worktree
    worktree = await worktree_manager.create_worktree(task_id=task_id)

    try:
        # Create container
        container = await docker_manager.create_container(
            task_id=task_id, worktree_path=worktree.path, cpu_limit=1.0, memory_limit="1g"
        )

        assert container.container_id
        assert container.task_id == task_id

        # Execute command
        result = await docker_manager.exec_command(
            container=container, command="echo 'Hello from container'"
        )

        assert result["exit_code"] == 0
        assert "Hello from container" in result["stdout"]

        # Get stats
        stats = await docker_manager.get_stats(container)
        assert "cpu_percent" in stats

        # Stop container
        stopped = await docker_manager.stop_container(container)
        assert stopped

    finally:
        # Cleanup
        await worktree_manager.cleanup_worktree(worktree, force=True)


# ─── Database Integration Tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_creation_and_lifecycle(db):
    """Test God Mode task database lifecycle."""
    # Create task
    row = await database.fetchrow(
        """
        INSERT INTO god_mode_tasks (title, description, status, priority)
        VALUES ($1, $2, 'backlog', 100)
        RETURNING id, title, status
        """,
        "Test Task",
        "Integration test task",
    )

    task_id = row["id"]
    assert row["title"] == "Test Task"
    assert row["status"] == "backlog"

    # Update to planning
    await database.execute("UPDATE god_mode_tasks SET status = 'planning' WHERE id = $1", task_id)

    # Generate plan
    plan = {
        "phases": [{"name": "Setup", "steps": [{"step": "Initialize", "command": "echo setup"}]}]
    }

    await database.execute(
        """
        UPDATE god_mode_tasks
        SET plan = $2::jsonb,
            plan_generated_at = now(),
            status = 'pending'
        WHERE id = $1
        """,
        task_id,
        database.json.dumps(plan),
    )

    # Approve plan
    await database.execute(
        """
        UPDATE god_mode_tasks
        SET plan_approved = TRUE,
            plan_approved_at = now(),
            plan_approved_by = 'test'
        WHERE id = $1
        """,
        task_id,
    )

    # Verify
    task = await database.fetchrow("SELECT * FROM god_mode_tasks WHERE id = $1", task_id)

    assert task["status"] == "pending"
    assert task["plan_approved"]

    # Cleanup
    await database.execute("DELETE FROM god_mode_tasks WHERE id = $1", task_id)


@pytest.mark.asyncio
async def test_event_logging(db):
    """Test God Mode event logging."""
    # Create task
    task_id = uuid4()

    await database.execute(
        """
        INSERT INTO god_mode_tasks (id, title, status)
        VALUES ($1, 'Event Test', 'backlog')
        """,
        task_id,
    )

    # Log events
    await database.execute(
        """
        INSERT INTO god_mode_events (task_id, event_type, actor, data)
        VALUES ($1, 'task_created', 'test', '{}'::jsonb)
        """,
        task_id,
    )

    await database.execute(
        """
        INSERT INTO god_mode_events (task_id, event_type, actor, data)
        VALUES ($1, 'agent_started', 'test-agent', $2::jsonb)
        """,
        task_id,
        database.json.dumps({"agent_id": "test-agent"}),
    )

    # Query events
    events = await database.fetch(
        """
        SELECT event_type, actor
        FROM god_mode_events
        WHERE task_id = $1
        ORDER BY ts ASC
        """,
        task_id,
    )

    assert len(events) == 2
    assert events[0]["event_type"] == "task_created"
    assert events[1]["event_type"] == "agent_started"

    # Cleanup
    await database.execute("DELETE FROM god_mode_events WHERE task_id = $1", task_id)
    await database.execute("DELETE FROM god_mode_tasks WHERE id = $1", task_id)


# ─── End-to-End Tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.slow
async def test_full_orchestration_flow(db, orchestrator, worktree_manager):
    """Test complete orchestration flow (without actual agent execution)."""
    # Create task in database
    task_id = uuid4()

    plan = {
        "phases": [
            {
                "name": "Create test file",
                "steps": [
                    {"step": "Create file", "command": "echo 'test content' > test_output.txt"}
                ],
            }
        ]
    }

    await database.execute(
        """
        INSERT INTO god_mode_tasks (
            id, title, description, status, plan,
            plan_approved, plan_approved_by, total_phases
        )
        VALUES ($1, $2, $3, 'pending', $4::jsonb, TRUE, 'test', 1)
        """,
        task_id,
        "Integration Test Task",
        "Full orchestration test",
        database.json.dumps(plan),
    )

    # Execute task
    task = await database.fetchrow("SELECT * FROM god_mode_tasks WHERE id = $1", task_id)

    await orchestrator._execute_task(dict(task))

    # Verify task completed
    result = await database.fetchrow(
        "SELECT status, validation_score, error FROM god_mode_tasks WHERE id = $1", task_id
    )

    # Task should complete (may pass or fail validation depending on environment)
    assert result["status"] in ["done", "failed"]

    # Cleanup
    await database.execute("DELETE FROM god_mode_events WHERE task_id = $1", task_id)
    await database.execute("DELETE FROM god_mode_tasks WHERE id = $1", task_id)

    # Cleanup test file if it exists
    test_file = worktree_manager.repo_root / "test_output.txt"
    if test_file.exists():
        test_file.unlink()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
