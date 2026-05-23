"""God Mode Control Center — Real-time Kanban UI.

Provides web-based dashboard for God Mode task orchestration:
- Kanban board with drag-and-drop (backlog → planning → pending → running → review → done)
- Real-time updates via Server-Sent Events (SSE)
- Task creation and plan approval
- Live agent progress monitoring
- Validation results visualization
- Manual intervention controls

Tech stack:
- FastAPI for backend API
- HTMX for dynamic updates
- Alpine.js for client-side interactivity
- Tailwind CSS for styling
- SSE for real-time event streaming

Routes:
- GET /godmode - Main dashboard
- GET /godmode/tasks - Task list (JSON)
- POST /godmode/tasks - Create new task
- GET /godmode/tasks/{id} - Task details
- POST /godmode/tasks/{id}/approve - Approve plan
- POST /godmode/tasks/{id}/cancel - Cancel task
- GET /godmode/events - SSE stream for real-time updates
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from core import database


router = APIRouter(prefix="/godmode", tags=["godmode"])


# ─── Request Models ───────────────────────────────────────────────────────


class CreateTaskRequest(BaseModel):
    title: str
    description: Optional[str] = None
    priority: int = 100


class ApproveTaskRequest(BaseModel):
    approved_by: str = "user"


# ─── API Routes ───────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render main God Mode dashboard."""
    return HTMLResponse(content=DASHBOARD_HTML)


@router.get("/tasks")
async def list_tasks(status: Optional[str] = None, limit: int = 50):
    """List God Mode tasks with optional status filter."""
    where_clause = "WHERE deleted_at IS NULL"
    args = []

    if status:
        args.append(status)
        where_clause += f" AND status = ${len(args)}"

    args.append(limit)

    rows = await database.fetch(
        f"""
        SELECT id, created_at, updated_at, title, description, status,
               priority, plan, plan_generated_at, plan_approved,
               agent_id, worktree_path, git_branch, current_phase,
               total_phases, progress_pct, validation_score,
               validation_results, tests_passed, tests_total,
               started_at, finished_at, duration_ms, error,
               commits, files_changed, retry_count, max_retries,
               validation_attempts, max_validation_attempts
        FROM god_mode_tasks
        {where_clause}
        ORDER BY priority ASC, created_at DESC
        LIMIT ${len(args)}
        """,
        *args,
    )

    tasks = []
    for row in rows:
        task = dict(row)
        # Convert UUID and datetime to strings
        task["id"] = str(task["id"])
        for key in ["created_at", "updated_at", "plan_generated_at", "started_at", "finished_at"]:
            if task.get(key):
                task[key] = task[key].isoformat()

        # Parse JSON fields
        for key in ["plan", "validation_results"]:
            if task.get(key) and isinstance(task[key], str):
                task[key] = json.loads(task[key])

        tasks.append(task)

    return {"tasks": tasks, "count": len(tasks)}


@router.post("/tasks")
async def create_task(req: CreateTaskRequest):
    """Create new God Mode task."""
    row = await database.fetchrow(
        """
        INSERT INTO god_mode_tasks (title, description, priority, status)
        VALUES ($1, $2, $3, 'backlog')
        RETURNING id, created_at, title, description, status, priority
        """,
        req.title,
        req.description,
        req.priority,
    )

    # Log event
    await database.execute(
        """
        INSERT INTO god_mode_events (task_id, event_type, actor, data)
        VALUES ($1, 'task_created', 'user', $2::jsonb)
        """,
        row["id"],
        json.dumps({"title": req.title}),
    )

    task = dict(row)
    task["id"] = str(task["id"])
    task["created_at"] = task["created_at"].isoformat()

    return {"task": task}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """Get detailed task information."""
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    row = await database.fetchrow(
        """
        SELECT id, created_at, updated_at, title, description, status,
               priority, plan, plan_generated_at, plan_approved,
               plan_approved_at, plan_approved_by, agent_id,
               worktree_path, git_branch, base_commit,
               docker_container_id, current_phase, total_phases,
               progress_pct, validation_score, validation_results,
               tests_passed, tests_total, validation_attempts,
               max_validation_attempts, started_at, finished_at,
               duration_ms, output, error, commits, files_changed,
               notify_telegram, last_notified_at, notification_count,
               retry_count, max_retries, last_feedback, metadata
        FROM god_mode_tasks
        WHERE id = $1 AND deleted_at IS NULL
        """,
        task_uuid,
    )

    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    task = dict(row)
    task["id"] = str(task["id"])

    # Convert timestamps
    for key in [
        "created_at",
        "updated_at",
        "plan_generated_at",
        "plan_approved_at",
        "started_at",
        "finished_at",
        "last_notified_at",
    ]:
        if task.get(key):
            task[key] = task[key].isoformat()

    # Parse JSON fields
    for key in ["plan", "validation_results", "metadata"]:
        if task.get(key) and isinstance(task[key], str):
            task[key] = json.loads(task[key])

    return {"task": task}


@router.post("/tasks/{task_id}/approve")
async def approve_task(task_id: str, req: ApproveTaskRequest):
    """Approve task plan and move to pending status."""
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    # Check task exists and has plan
    task = await database.fetchrow(
        "SELECT id, status, plan FROM god_mode_tasks WHERE id = $1 AND deleted_at IS NULL",
        task_uuid,
    )

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task["status"] != "pending":
        raise HTTPException(
            status_code=400, detail=f"Task status is {task['status']}, expected 'pending'"
        )

    if not task["plan"]:
        raise HTTPException(status_code=400, detail="Task has no plan to approve")

    # Approve and move to pending
    await database.execute(
        """
        UPDATE god_mode_tasks
        SET plan_approved = TRUE,
            plan_approved_at = now(),
            plan_approved_by = $2,
            status = 'pending',
            updated_at = now()
        WHERE id = $1
        """,
        task_uuid,
        req.approved_by,
    )

    # Log event
    await database.execute(
        """
        INSERT INTO god_mode_events (task_id, event_type, actor, data)
        VALUES ($1, 'plan_approved', $2, '{}'::jsonb)
        """,
        task_uuid,
        req.approved_by,
    )

    return {"success": True, "message": "Plan approved, task queued for execution"}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """Cancel a task."""
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    await database.execute(
        """
        UPDATE god_mode_tasks
        SET status = 'cancelled',
            updated_at = now()
        WHERE id = $1 AND deleted_at IS NULL
        """,
        task_uuid,
    )

    # Log event
    await database.execute(
        """
        INSERT INTO god_mode_events (task_id, event_type, actor, data)
        VALUES ($1, 'task_cancelled', 'user', '{}'::jsonb)
        """,
        task_uuid,
    )

    return {"success": True, "message": "Task cancelled"}


@router.get("/events")
async def event_stream(request: Request):
    """Server-Sent Events stream for real-time updates."""

    async def generate():
        last_event_id = 0

        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                break

            # Fetch new events
            rows = await database.fetch(
                """
                SELECT id, ts, task_id, phase_id, event_type, actor, data
                FROM god_mode_events
                WHERE id > $1 AND notified = FALSE
                ORDER BY id ASC
                LIMIT 50
                """,
                last_event_id,
            )

            for row in rows:
                event_id = row["id"]
                event_data = {
                    "id": event_id,
                    "ts": row["ts"].isoformat(),
                    "task_id": str(row["task_id"]) if row["task_id"] else None,
                    "phase_id": str(row["phase_id"]) if row["phase_id"] else None,
                    "event_type": row["event_type"],
                    "actor": row["actor"],
                    "data": row["data"]
                    if isinstance(row["data"], dict)
                    else json.loads(row["data"] or "{}"),
                }

                # Send SSE event
                yield f"id: {event_id}\n"
                yield f"event: {row['event_type']}\n"
                yield f"data: {json.dumps(event_data)}\n\n"

                last_event_id = event_id

                # Mark as notified
                await database.execute(
                    "UPDATE god_mode_events SET notified = TRUE WHERE id = $1", event_id
                )

            # Wait before next poll
            await asyncio.sleep(2)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── Dashboard HTML ───────────────────────────────────────────────────────


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JARVIS God Mode — Control Center</title>
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <script src="https://unpkg.com/alpinejs@3.13.3/dist/cdn.min.js" defer></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        [x-cloak] { display: none !important; }
        .status-backlog { @apply bg-gray-100 border-gray-300; }
        .status-planning { @apply bg-blue-100 border-blue-300; }
        .status-pending { @apply bg-yellow-100 border-yellow-300; }
        .status-running { @apply bg-purple-100 border-purple-300; }
        .status-review { @apply bg-orange-100 border-orange-300; }
        .status-done { @apply bg-green-100 border-green-300; }
        .status-failed { @apply bg-red-100 border-red-300; }
        .status-cancelled { @apply bg-gray-200 border-gray-400; }
    </style>
</head>
<body class="bg-gray-50" x-data="godmode()">
    <div class="min-h-screen">
        <!-- Header -->
        <header class="bg-gray-900 text-white shadow-lg">
            <div class="container mx-auto px-4 py-4">
                <div class="flex items-center justify-between">
                    <div>
                        <h1 class="text-2xl font-bold">JARVIS God Mode</h1>
                        <p class="text-gray-400 text-sm">Autonomous Agent Orchestration</p>
                    </div>
                    <div class="flex items-center gap-4">
                        <div class="text-right">
                            <div class="text-sm text-gray-400">Active Tasks</div>
                            <div class="text-xl font-bold" x-text="tasks.filter(t => t.status === 'running').length"></div>
                        </div>
                        <button
                            @click="showCreateModal = true"
                            class="bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded-lg font-medium transition"
                        >
                            + New Task
                        </button>
                    </div>
                </div>
            </div>
        </header>

        <!-- Kanban Board -->
        <main class="container mx-auto px-4 py-8">
            <div class="grid grid-cols-7 gap-4">
                <template x-for="status in statuses" :key="status">
                    <div class="bg-white rounded-lg shadow">
                        <div class="p-4 border-b">
                            <h3 class="font-bold text-gray-700 uppercase text-sm" x-text="status"></h3>
                            <div class="text-xs text-gray-500 mt-1">
                                <span x-text="tasks.filter(t => t.status === status).length"></span> tasks
                            </div>
                        </div>
                        <div class="p-2 space-y-2 min-h-[400px]">
                            <template x-for="task in tasks.filter(t => t.status === status)" :key="task.id">
                                <div
                                    @click="selectTask(task)"
                                    class="p-3 rounded border-2 cursor-pointer hover:shadow-md transition"
                                    :class="'status-' + task.status"
                                >
                                    <div class="font-medium text-sm mb-1" x-text="task.title"></div>
                                    <div class="text-xs text-gray-600 mb-2" x-text="task.description?.substring(0, 60) + '...'"></div>
                                    <div class="flex items-center justify-between text-xs">
                                        <span class="text-gray-500" x-text="'#' + task.id.substring(0, 8)"></span>
                                        <template x-if="task.validation_score">
                                            <span
                                                class="px-2 py-1 rounded font-medium"
                                                :class="task.validation_score >= 85 ? 'bg-green-200 text-green-800' : task.validation_score >= 70 ? 'bg-yellow-200 text-yellow-800' : 'bg-red-200 text-red-800'"
                                                x-text="task.validation_score"
                                            ></span>
                                        </template>
                                    </div>
                                    <template x-if="task.status === 'running' && task.progress_pct">
                                        <div class="mt-2">
                                            <div class="w-full bg-gray-200 rounded-full h-1.5">
                                                <div class="bg-blue-600 h-1.5 rounded-full" :style="'width: ' + task.progress_pct + '%'"></div>
                                            </div>
                                        </div>
                                    </template>
                                </div>
                            </template>
                        </div>
                    </div>
                </template>
            </div>
        </main>

        <!-- Create Task Modal -->
        <div x-show="showCreateModal" x-cloak class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div class="bg-white rounded-lg shadow-xl p-6 w-full max-w-lg" @click.away="showCreateModal = false">
                <h2 class="text-xl font-bold mb-4">Create New Task</h2>
                <form @submit.prevent="createTask()">
                    <div class="mb-4">
                        <label class="block text-sm font-medium mb-2">Title</label>
                        <input
                            type="text"
                            x-model="newTask.title"
                            class="w-full border rounded px-3 py-2"
                            required
                        />
                    </div>
                    <div class="mb-4">
                        <label class="block text-sm font-medium mb-2">Description</label>
                        <textarea
                            x-model="newTask.description"
                            class="w-full border rounded px-3 py-2"
                            rows="4"
                        ></textarea>
                    </div>
                    <div class="mb-4">
                        <label class="block text-sm font-medium mb-2">Priority</label>
                        <input
                            type="number"
                            x-model="newTask.priority"
                            class="w-full border rounded px-3 py-2"
                            min="1"
                            max="999"
                        />
                    </div>
                    <div class="flex justify-end gap-2">
                        <button
                            type="button"
                            @click="showCreateModal = false"
                            class="px-4 py-2 border rounded hover:bg-gray-50"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            class="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
                        >
                            Create
                        </button>
                    </div>
                </form>
            </div>
        </div>

        <!-- Task Detail Modal -->
        <div x-show="selectedTask" x-cloak class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div class="bg-white rounded-lg shadow-xl p-6 w-full max-w-3xl max-h-[90vh] overflow-y-auto" @click.away="selectedTask = null">
                <template x-if="selectedTask">
                    <div>
                        <div class="flex items-start justify-between mb-4">
                            <div>
                                <h2 class="text-2xl font-bold" x-text="selectedTask.title"></h2>
                                <p class="text-gray-600 text-sm mt-1" x-text="'#' + selectedTask.id"></p>
                            </div>
                            <span
                                class="px-3 py-1 rounded-full text-sm font-medium"
                                :class="'status-' + selectedTask.status"
                                x-text="selectedTask.status"
                            ></span>
                        </div>

                        <div class="mb-4">
                            <p class="text-gray-700" x-text="selectedTask.description"></p>
                        </div>

                        <template x-if="selectedTask.status === 'pending' && !selectedTask.plan_approved">
                            <div class="mb-4 p-4 bg-yellow-50 border border-yellow-200 rounded">
                                <p class="text-sm mb-2">Plan generated and awaiting approval</p>
                                <button
                                    @click="approveTask(selectedTask.id)"
                                    class="px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700"
                                >
                                    Approve Plan
                                </button>
                            </div>
                        </template>

                        <template x-if="selectedTask.validation_score">
                            <div class="mb-4 p-4 bg-gray-50 rounded">
                                <h3 class="font-bold mb-2">Validation Score</h3>
                                <div class="text-3xl font-bold" x-text="selectedTask.validation_score + '/100'"></div>
                            </div>
                        </template>

                        <div class="flex justify-end gap-2">
                            <button
                                @click="selectedTask = null"
                                class="px-4 py-2 border rounded hover:bg-gray-50"
                            >
                                Close
                            </button>
                        </div>
                    </div>
                </template>
            </div>
        </div>
    </div>

    <script>
        function godmode() {
            return {
                tasks: [],
                statuses: ['backlog', 'planning', 'pending', 'running', 'review', 'done', 'failed'],
                showCreateModal: false,
                selectedTask: null,
                newTask: {
                    title: '',
                    description: '',
                    priority: 100
                },

                async init() {
                    await this.loadTasks();
                    this.connectSSE();
                },

                async loadTasks() {
                    const response = await fetch('/godmode/tasks');
                    const data = await response.json();
                    this.tasks = data.tasks;
                },

                connectSSE() {
                    const eventSource = new EventSource('/godmode/events');

                    eventSource.addEventListener('task_created', () => this.loadTasks());
                    eventSource.addEventListener('agent_started', () => this.loadTasks());
                    eventSource.addEventListener('task_complete', () => this.loadTasks());
                    eventSource.addEventListener('task_failed', () => this.loadTasks());
                    eventSource.addEventListener('plan_approved', () => this.loadTasks());

                    eventSource.onerror = () => {
                        console.error('SSE connection error');
                        setTimeout(() => this.connectSSE(), 5000);
                    };
                },

                async createTask() {
                    const response = await fetch('/godmode/tasks', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(this.newTask)
                    });

                    if (response.ok) {
                        this.showCreateModal = false;
                        this.newTask = { title: '', description: '', priority: 100 };
                        await this.loadTasks();
                    }
                },

                async approveTask(taskId) {
                    const response = await fetch(`/godmode/tasks/${taskId}/approve`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ approved_by: 'user' })
                    });

                    if (response.ok) {
                        this.selectedTask = null;
                        await this.loadTasks();
                    }
                },

                selectTask(task) {
                    this.selectedTask = task;
                }
            }
        }
    </script>
</body>
</html>
"""
