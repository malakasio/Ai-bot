"""God Mode Orchestrator — Autonomous Multi-Agent Task Execution.

Coordinates the full lifecycle of God Mode tasks:
1. Task creation and planning
2. Worktree isolation setup
3. Agent execution with progress tracking
4. Validation pipeline with self-healing
5. Merge back to main or cleanup on failure
6. Telegram notifications at each stage

Architecture:
- Pulls tasks from god_mode_tasks table (status='pending', plan_approved=TRUE)
- Creates isolated git worktree for each task
- Spawns agent in worktree with execution plan
- Monitors progress and runs validation pipeline
- Self-heals: if validation score < 70, provides feedback and retries (max 3 attempts)
- On success: merges to main, sends completion notification, cleans up worktree
- On failure: sends failure notification, preserves worktree for debugging

Integration points:
- godmode/isolation/worktree.py: Git isolation
- godmode/validation/pipeline.py: Quality scoring
- godmode/reporting/telegram_bot.py: User notifications
- core/database.py: PostgreSQL persistence
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import UUID

from core import database
from godmode.isolation import WorktreeManager, Worktree
from godmode.validation.pipeline import ValidationPipeline
from godmode.reporting.telegram_bot import get_reporter


@dataclass
class TaskExecution:
    """Represents a running God Mode task."""

    task_id: UUID
    worktree: Worktree
    agent_id: str
    plan: dict
    current_phase: int
    total_phases: int
    started_at: float
    validation_attempts: int = 0


class GodModeOrchestrator:
    """Orchestrates autonomous multi-agent task execution."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.worktree_manager = WorktreeManager(repo_root)
        self.reporter = get_reporter()
        self.running_tasks: dict[UUID, TaskExecution] = {}

    async def run_forever(self, poll_interval: int = 30):
        """
        Main orchestrator loop.

        Polls for pending approved tasks and executes them.
        Runs until interrupted.
        """
        print(f"[godmode] Orchestrator started (poll_interval={poll_interval}s)")

        while True:
            try:
                # Claim next pending task
                task = await self._claim_next_task()

                if task:
                    # Execute task in background
                    asyncio.create_task(self._execute_task(task))

                # Wait before next poll
                await asyncio.sleep(poll_interval)

            except KeyboardInterrupt:
                print("[godmode] Orchestrator interrupted, shutting down...")
                break
            except Exception as e:
                print(f"[godmode] Orchestrator error: {e}", file=sys.stderr)
                await asyncio.sleep(poll_interval)

    async def _claim_next_task(self) -> Optional[dict]:
        """
        Atomically claim next pending approved task.

        Returns task dict or None if no tasks available.
        """
        # Generate unique agent ID
        import socket

        agent_id = f"godmode-{socket.gethostname()}-{os.getpid()}"

        row = await database.fetchrow(
            """
            UPDATE god_mode_tasks
            SET
                agent_id = $1,
                status = 'running',
                started_at = now(),
                updated_at = now()
            WHERE id = (
                SELECT id FROM god_mode_tasks
                WHERE status = 'pending'
                  AND plan_approved = TRUE
                  AND deleted_at IS NULL
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, title, description, plan, priority,
                      current_phase, total_phases, validation_attempts,
                      max_validation_attempts, retry_count, max_retries
            """,
            agent_id,
        )

        if not row:
            return None

        # Log event
        await database.execute(
            """
            INSERT INTO god_mode_events (task_id, event_type, actor, data)
            VALUES ($1, 'agent_started', $2, $3::jsonb)
            """,
            row["id"],
            agent_id,
            json.dumps({"agent_id": agent_id}),
        )

        return dict(row)

    async def _execute_task(self, task: dict):
        """
        Execute a single God Mode task end-to-end.

        Steps:
        1. Create isolated worktree
        2. Send task started notification
        3. Execute plan phases
        4. Run validation pipeline
        5. Self-heal if needed (retry with feedback)
        6. Merge to main on success
        7. Cleanup worktree
        8. Send completion/failure notification
        """
        task_id = task["id"]
        title = task["title"]

        # Parse plan if it's a JSON string
        plan = task["plan"]
        if isinstance(plan, str):
            try:
                plan = json.loads(plan)
            except (json.JSONDecodeError, TypeError):
                plan = {}
        elif plan is None:
            plan = {}

        agent_id = f"godmode-agent-{task_id}"

        print(f"[godmode] Executing task {task_id}: {title}")

        worktree = None
        validation_result = None

        try:
            # 1. Create isolated worktree
            worktree = await self.worktree_manager.create_worktree(
                task_id=task_id, base_branch="main", new_branch=f"godmode/task-{task_id}"
            )

            # Update task with worktree info
            await database.execute(
                """
                UPDATE god_mode_tasks
                SET worktree_path = $2,
                    git_branch = $3,
                    base_commit = $4,
                    updated_at = now()
                WHERE id = $1
                """,
                task_id,
                str(worktree.path),
                worktree.branch,
                worktree.base_commit,
            )

            # 2. Send task started notification
            await self.reporter.notify_task_started(
                task_id=task_id, title=title, agent_id=agent_id, branch=worktree.branch, plan=plan
            )

            # 3. Execute plan phases
            phases = plan.get("phases", [])
            total_phases = len(phases)

            await database.execute(
                "UPDATE god_mode_tasks SET total_phases = $2 WHERE id = $1", task_id, total_phases
            )

            for phase_idx, phase in enumerate(phases):
                print(
                    f"[godmode] Task {task_id} - Phase {phase_idx + 1}/{total_phases}: {phase.get('name')}"
                )

                # Update current phase
                await database.execute(
                    """
                    UPDATE god_mode_tasks
                    SET current_phase = $2,
                        progress_pct = $3,
                        updated_at = now()
                    WHERE id = $1
                    """,
                    task_id,
                    phase_idx,
                    int((phase_idx / total_phases) * 100),
                )

                # Execute phase steps
                success = await self._execute_phase(task_id, worktree, phase_idx, phase)

                if not success:
                    raise RuntimeError(f"Phase {phase_idx + 1} failed")

                # Send progress notification every 5 minutes or on phase completion
                if phase_idx % 3 == 0 or phase_idx == total_phases - 1:
                    await self.reporter.notify_progress(
                        task_id=task_id,
                        title=title,
                        current_phase=phase_idx,
                        total_phases=total_phases,
                        progress_pct=int(((phase_idx + 1) / total_phases) * 100),
                    )

            # 4. Get changes summary
            changes = await self.worktree_manager.get_changes(worktree)
            files_changed = changes.get("files_changed", [])
            commits = changes.get("commits", [])

            # 5. Run validation pipeline
            validator = ValidationPipeline(worktree.path)
            validation_result = await validator.validate(
                task_id=task_id,
                files_changed=files_changed,
                commits=commits,
                requirements=plan.get("requirements"),
            )

            # Update task with validation results
            await database.execute(
                """
                UPDATE god_mode_tasks
                SET validation_score = $2,
                    validation_results = $3::jsonb,
                    validation_attempts = validation_attempts + 1,
                    updated_at = now()
                WHERE id = $1
                """,
                task_id,
                validation_result.score,
                json.dumps(
                    {
                        "score": validation_result.score,
                        "correctness": validation_result.correctness_score,
                        "completeness": validation_result.completeness_score,
                        "efficiency": validation_result.efficiency_score,
                        "safety": validation_result.safety_score,
                        "syntax": validation_result.syntax,
                        "tests": validation_result.tests,
                        "security": validation_result.security,
                        "logs": validation_result.logs,
                        "feedback": validation_result.feedback,
                        "timestamp": validation_result.timestamp,
                    }
                ),
            )

            # Send validation notification
            await self.reporter.notify_validation_results(
                task_id=task_id,
                title=title,
                score=validation_result.score,
                validation_results={
                    "syntax": validation_result.syntax,
                    "tests": validation_result.tests,
                    "security": validation_result.security,
                },
                accepted=validation_result.passed,
            )

            # 6. Self-heal if validation failed
            if not validation_result.passed:
                current_attempts = task["validation_attempts"] + 1
                max_attempts = task["max_validation_attempts"]

                if current_attempts < max_attempts:
                    # Retry with feedback
                    feedback_text = "\n".join(validation_result.feedback)

                    await database.execute(
                        """
                        UPDATE god_mode_tasks
                        SET last_feedback = $2,
                            status = 'pending',
                            agent_id = NULL,
                            updated_at = now()
                        WHERE id = $1
                        """,
                        task_id,
                        feedback_text,
                    )

                    print(
                        f"[godmode] Task {task_id} validation failed (score={validation_result.score}), retrying with feedback"
                    )

                    # Cleanup worktree for retry
                    await self.worktree_manager.cleanup_worktree(worktree, force=True)

                    return  # Will be picked up again in next poll
                else:
                    # Max attempts reached, mark as failed
                    raise RuntimeError(
                        f"Validation failed after {max_attempts} attempts (score={validation_result.score})"
                    )

            # 7. Merge to main on success
            merge_result = await self.worktree_manager.merge_worktree(
                worktree=worktree, target_branch="main", squash=False
            )

            if not merge_result["success"]:
                raise RuntimeError(f"Merge failed: {merge_result.get('error')}")

            merge_commit = merge_result["merge_commit"]

            # 8. Mark task as done
            duration_ms = int((time.time() - worktree.created_at) * 1000)

            await database.execute(
                """
                UPDATE god_mode_tasks
                SET status = 'done',
                    finished_at = now(),
                    duration_ms = $2,
                    commits = $3,
                    files_changed = $4,
                    progress_pct = 100,
                    updated_at = now()
                WHERE id = $1
                """,
                task_id,
                duration_ms,
                commits,
                files_changed,
            )

            # Log completion event
            await database.execute(
                """
                INSERT INTO god_mode_events (task_id, event_type, actor, data)
                VALUES ($1, 'task_complete', $2, $3::jsonb)
                """,
                task_id,
                agent_id,
                json.dumps(
                    {
                        "score": validation_result.score,
                        "duration_ms": duration_ms,
                        "merge_commit": merge_commit,
                    }
                ),
            )

            # Send completion notification
            tests = validation_result.tests
            await self.reporter.notify_task_complete(
                task_id=task_id,
                title=title,
                score=validation_result.score,
                duration_ms=duration_ms,
                tests_passed=tests.get("passed", 0),
                tests_total=tests.get("total", 0),
                commits=commits,
                files_changed=files_changed,
                merge_commit=merge_commit,
            )

            # 9. Cleanup worktree
            await self.worktree_manager.cleanup_worktree(worktree, force=False)

            print(
                f"[godmode] Task {task_id} completed successfully (score={validation_result.score})"
            )

        except Exception as e:
            # Task failed
            error_msg = str(e)
            print(f"[godmode] Task {task_id} failed: {error_msg}", file=sys.stderr)

            # Update task status
            await database.execute(
                """
                UPDATE god_mode_tasks
                SET status = 'failed',
                    error = $2,
                    finished_at = now(),
                    updated_at = now()
                WHERE id = $1
                """,
                task_id,
                error_msg,
            )

            # Log failure event
            await database.execute(
                """
                INSERT INTO god_mode_events (task_id, event_type, actor, data)
                VALUES ($1, 'task_failed', $2, $3::jsonb)
                """,
                task_id,
                agent_id,
                json.dumps({"error": error_msg}),
            )

            # Send failure notification
            score = validation_result.score if validation_result else 0
            attempts = task["validation_attempts"] + 1

            await self.reporter.notify_task_failed(
                task_id=task_id,
                title=title,
                score=score,
                attempts=attempts,
                max_attempts=task["max_validation_attempts"],
                error=error_msg,
                validation_results=validation_result.__dict__ if validation_result else None,
            )

            # Preserve worktree for debugging (don't cleanup on failure)
            if worktree:
                print(f"[godmode] Worktree preserved for debugging: {worktree.path}")

    async def _execute_phase(
        self, task_id: UUID, worktree: Worktree, phase_idx: int, phase: dict
    ) -> bool:
        """
        Execute a single phase of the plan.

        Returns True on success, False on failure.
        """
        phase_name = phase.get("name", f"Phase {phase_idx + 1}")
        steps = phase.get("steps", [])

        print(f"[godmode] Executing phase: {phase_name}")

        for step_idx, step in enumerate(steps):
            step_desc = step.get("step", f"Step {step_idx + 1}")
            command = step.get("command")

            if not command:
                print(f"[godmode] Skipping step (no command): {step_desc}")
                continue

            print(f"[godmode]   Step {step_idx + 1}/{len(steps)}: {step_desc}")
            print(f"[godmode]   Command: {command}")

            # Validate command
            allowed_commands = ["git", "npm", "python", "python3", "pytest", "ruff", "mypy", "pip", "poetry"]
            try:
                args = shlex.split(command)
            except ValueError as e:
                print(f"[godmode]   Step failed: Invalid command syntax: {e}")
                return False

            first_word = args[0] if args else ""

            if first_word not in allowed_commands:
                print(f"[godmode]   Step failed: Command not allowed: {first_word}")
                return False

            # Execute command in worktree using argument array (safe)
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=worktree.path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                print(f"[godmode]   Step failed (exit code {proc.returncode})")
                print(f"[godmode]   stderr: {stderr.decode()[:500]}")
                return False

            print("[godmode]   Step completed successfully")

        return True


async def main():
    """Entry point for standalone orchestrator daemon."""
    repo_root = Path("/root/Ai-bot")
    orchestrator = GodModeOrchestrator(repo_root)

    poll_interval = int(os.getenv("GODMODE_POLL_INTERVAL", "30"))

    await orchestrator.run_forever(poll_interval=poll_interval)


if __name__ == "__main__":
    asyncio.run(main())
