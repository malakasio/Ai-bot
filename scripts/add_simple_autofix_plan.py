#!/usr/bin/env python3
"""Add simple auto-fix plan that will work reliably."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from core import database

async def add_simple_plan():
    task_id = "2398f072-5587-4d22-9f29-b4fe6f0ab221"

    plan = {
        "phases": [
            {
                "name": "Scan for errors",
                "steps": [
                    {
                        "step": "Run ruff check",
                        "command": "ruff check . --output-format=text > /tmp/ruff_before.txt 2>&1 || true"
                    },
                    {
                        "step": "Count errors",
                        "command": "wc -l /tmp/ruff_before.txt"
                    }
                ]
            },
            {
                "name": "Auto-fix errors",
                "steps": [
                    {
                        "step": "Apply safe fixes",
                        "command": "ruff check . --fix 2>&1 | tee /tmp/ruff_fix.log"
                    },
                    {
                        "step": "Apply unsafe fixes",
                        "command": "ruff check . --fix --unsafe-fixes 2>&1 | tee -a /tmp/ruff_fix.log"
                    },
                    {
                        "step": "Format code",
                        "command": "ruff format . 2>&1 | tee /tmp/ruff_format.log"
                    }
                ]
            },
            {
                "name": "Verify fixes",
                "steps": [
                    {
                        "step": "Re-run ruff check",
                        "command": "ruff check . --output-format=text > /tmp/ruff_after.txt 2>&1 || true"
                    },
                    {
                        "step": "Count remaining errors",
                        "command": "wc -l /tmp/ruff_after.txt"
                    },
                    {
                        "step": "Show improvement",
                        "command": "echo 'Before:' && wc -l /tmp/ruff_before.txt && echo 'After:' && wc -l /tmp/ruff_after.txt"
                    }
                ]
            },
            {
                "name": "Commit changes",
                "steps": [
                    {
                        "step": "Check what changed",
                        "command": "git status --short"
                    },
                    {
                        "step": "Stage all changes",
                        "command": "git add -A"
                    },
                    {
                        "step": "Commit fixes",
                        "command": "git commit -m 'fix: auto-fix ruff errors and format code' -m 'Applied ruff --fix and ruff format to entire codebase' -m 'Co-Authored-By: JARVIS <jarvis@local>' || echo 'No changes to commit'"
                    }
                ]
            }
        ]
    }

    await database.init()

    try:
        await database.execute(
            """
            UPDATE god_mode_tasks
            SET plan = $2::jsonb,
                plan_generated_at = now(),
                status = 'pending',
                total_phases = $3
            WHERE id = $1
            """,
            task_id,
            json.dumps(plan),
            len(plan["phases"])
        )

        print(f"✅ Simple auto-fix plan added to task {task_id}")
        print(f"\n📋 Plan includes {len(plan['phases'])} phases:")
        for i, phase in enumerate(plan["phases"], 1):
            print(f"  {i}. {phase['name']} ({len(phase['steps'])} steps)")

    finally:
        await database.close()

if __name__ == "__main__":
    asyncio.run(add_simple_plan())
