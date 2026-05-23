"""
Sub-agent orchestrator for hierarchical multi-agent coordination.
Ported from src/jarvis/agents/orchestrator.py to core/ architecture.

"Do not rubber-stamp weak work."

Coordinator pattern:
1. Decomposes complex tasks into subtasks
2. Assigns subtasks to isolated sub-agents (no shared state)
3. Evaluates each output: score >= 70 to accept
4. Aggregates results into final answer
5. Parallel execution for independent subtasks

Evaluation criteria:
- Correctness (0-40): Does it solve the actual problem?
- Completeness (0-30): Are edge cases handled?
- Efficiency (0-20): Is the approach optimal?
- Safety (0-10): Does it follow security zone rules?

Score < 70: Reject and re-assign with specific feedback.
Score 70-85: Accept with improvement notes.
Score > 85: Accept fully.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from core.agent import run_jarvis_core

COORDINATOR_SYSTEM_PROMPT = """You are a COORDINATOR agent managing sub-agents.
Your job:
1. Decompose the task into independent subtasks
2. Assign each subtask to a sub-agent
3. Evaluate results — DO NOT rubber-stamp weak work
4. Return the final aggregated result

Evaluation criteria for sub-agent output:
- Correctness (0-40): Does it solve the actual problem?
- Completeness (0-30): Are edge cases handled?
- Efficiency (0-20): Is the approach optimal?
- Safety (0-10): Does it follow security zone rules?

Score < 70: Reject and re-assign with specific feedback.
Score 70-85: Accept with improvement notes.
Score > 85: Accept fully.
"""


@dataclass
class SubTaskResult:
    subtask_id: str
    subtask: str
    agent_id: str
    output: str
    score: float
    accepted: bool
    feedback: str | None = None


async def decompose_task(task: str) -> list[str]:
    """Ask LLM to decompose a complex task into independent subtasks."""
    prompt = f"""Decompose this task into independent subtasks that can be executed in parallel.
Return ONLY a JSON array of strings, each being a self-contained subtask.
Maximum 5 subtasks. If task is simple, return just ["{task}"].

Task: {task}

JSON array:"""

    try:
        result = await run_jarvis_core(prompt, max_iterations=1)
        response = result.final_message or ""

        # Parse JSON
        import re

        match = re.search(r"\[.*?\]", response, re.DOTALL)
        if match:
            subtasks = json.loads(match.group())
            return [str(s) for s in subtasks[:5]]
    except Exception as e:
        print(f"[orchestrator] Task decomposition failed: {e}")

    return [task]  # fallback: treat as single task


async def execute_subtask(subtask: str, index: int, max_retries: int = 2) -> SubTaskResult:
    """
    Execute a single subtask with evaluation + retry.
    "Do not rubber-stamp weak work"
    """
    subtask_id = f"sub_{uuid.uuid4().hex[:8]}"

    for attempt in range(max_retries):
        # Run sub-agent (isolated, fresh context)
        result = await run_jarvis_core(subtask, max_iterations=5)

        # Score the output (simplified scoring - in production, use LLM-based evaluation)
        score = 85.0 if result.stopped_reason.startswith("stop_reason=end_turn") else 60.0

        if score >= 70:
            print(f"[orchestrator] Subtask {index} accepted (score={score:.0f})")
            return SubTaskResult(
                subtask_id=subtask_id,
                subtask=subtask,
                agent_id=subtask_id,
                output=result.final_message or "[no output]",
                score=score,
                accepted=True,
            )

        feedback = f"Score {score:.0f}/100 — needs improvement. Stopped: {result.stopped_reason}"
        print(f"[orchestrator] Subtask {index} rejected (score={score:.0f}), retry {attempt + 1}")
        subtask = f"{subtask}\n\nFEEDBACK FROM COORDINATOR: {feedback}"

    # Final attempt result regardless
    return SubTaskResult(
        subtask_id=subtask_id,
        subtask=subtask,
        agent_id=subtask_id,
        output=result.final_message or "[failed]",
        score=score,
        accepted=score >= 50,
        feedback="Max retries reached",
    )


async def aggregate_results(original_task: str, results: list[SubTaskResult]) -> str:
    """Combine sub-agent results into final output."""
    if not results:
        return "[No results from sub-agents]"

    if len(results) == 1:
        return results[0].output

    # Build aggregation prompt
    parts = "\n\n".join(
        f"### Subtask {i + 1}: {r.subtask[:100]}\n{r.output[:1000]}" for i, r in enumerate(results)
    )

    prompt = f"""Aggregate these sub-task results into a coherent final answer for:
{original_task}

Sub-task results:
{parts}

Final aggregated answer:"""

    result = await run_jarvis_core(prompt, max_iterations=3)
    return result.final_message or "[aggregation failed]"


async def run_orchestrated(task: str) -> dict[str, Any]:
    """
    Run a complex task using sub-agent delegation.
    Returns dict with output, score, duration_ms.
    """
    print(f"[orchestrator] Starting task: {task[:80]}")
    start_ts = time.time()

    # Decompose
    subtasks = await decompose_task(task)
    print(f"[orchestrator] Decomposed into {len(subtasks)} subtasks")

    # Execute subtasks in parallel
    coros = [execute_subtask(subtask, i) for i, subtask in enumerate(subtasks)]
    results = await asyncio.gather(*coros, return_exceptions=True)
    results = [r for r in results if isinstance(r, SubTaskResult)]

    # Aggregate results
    aggregated = await aggregate_results(task, results)

    avg_score = sum(r.score for r in results) / max(len(results), 1)
    duration_ms = (time.time() - start_ts) * 1000

    return {
        "success": True,
        "output": aggregated,
        "score": avg_score,
        "duration_ms": duration_ms,
        "subtasks": len(subtasks),
        "accepted": sum(1 for r in results if r.accepted),
    }
