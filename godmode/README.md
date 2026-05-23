# God Mode — Autonomous Agent Orchestration System

**Status:** ✅ Implementation Complete

God Mode is JARVIS's autonomous multi-agent orchestration system that enables parallel, isolated task execution with self-healing validation and automated reporting.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     God Mode Control Center                      │
│              (Web UI: Kanban + Real-time Updates)               │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Orchestrator Daemon                           │
│  • Polls for approved tasks (SELECT FOR UPDATE SKIP LOCKED)     │
│  • Creates isolated worktrees                                    │
│  • Spawns agents in Docker containers (optional)                │
│  • Monitors progress and runs validation                         │
│  • Self-heals with feedback loop (max 3 attempts)               │
│  • Merges to main on success, preserves on failure              │
└─────┬───────────────────┬───────────────────┬───────────────────┘
      │                   │                   │
      ▼                   ▼                   ▼
┌──────────┐      ┌──────────────┐    ┌─────────────┐
│ Worktree │      │  Validation  │    │  Telegram   │
│ Isolation│      │   Pipeline   │    │  Reporter   │
└──────────┘      └──────────────┘    └─────────────┘
```

## Components

### 1. Database Schema (`scripts/init_godmode_db.sql`)

Three core tables:

- **god_mode_tasks**: Main orchestration table
  - Status: backlog → planning → pending → running → review → done/failed
  - Plan approval workflow
  - Validation scoring (0-100)
  - Retry and self-healing counters

- **god_mode_phases**: Individual phase tracking within tasks

- **god_mode_events**: Audit log for real-time UI updates via SSE

### 2. Git Worktree Isolation (`godmode/isolation/worktree.py`)

Provides isolated git worktrees for parallel agent execution:

```python
worktree_manager = WorktreeManager(repo_root)

# Create isolated worktree
worktree = await worktree_manager.create_worktree(
    task_id=task_id,
    base_branch="main"
)
# → Creates .claude/worktrees/task-{uuid}/ on branch godmode/task-{uuid}

# Get changes
changes = await worktree_manager.get_changes(worktree)

# Merge back to main
result = await worktree_manager.merge_worktree(worktree, target_branch="main")

# Cleanup
await worktree_manager.cleanup_worktree(worktree)
```

### 3. Docker Sandboxing (`godmode/isolation/docker.py`)

Optional container isolation with resource limits:

```python
docker_manager = DockerManager()

# Create container
container = await docker_manager.create_container(
    task_id=task_id,
    worktree_path=worktree.path,
    cpu_limit=2.0,
    memory_limit="4g"
)

# Execute command
result = await docker_manager.exec_command(
    container=container,
    command="pytest tests/"
)

# Cleanup
await docker_manager.stop_container(container)
```

### 4. Validation Pipeline (`godmode/validation/pipeline.py`)

Multi-dimensional quality scoring:

- **Correctness (0-40)**: Syntax checks (ruff, tsc, shellcheck) + test results
- **Completeness (0-30)**: Requirements met, edge cases handled
- **Efficiency (0-20)**: Performance acceptable, no obvious waste
- **Safety (0-10)**: Security scan (bandit, secrets), no vulnerabilities

```python
validator = ValidationPipeline(worktree.path)

result = await validator.validate(
    task_id=task_id,
    files_changed=["core/agent.py", "tests/test_agent.py"],
    commits=["abc123", "def456"]
)

# result.score: 0-100
# result.passed: score >= 70
# result.feedback: ["Fix 2 syntax errors", "Add unit tests"]
```

**Self-healing logic:**
- Score < 70: Reject, provide feedback, retry (max 3 attempts)
- Score 70-85: Accept with improvement notes
- Score > 85: Accept

### 5. Telegram Reporting (`godmode/reporting/telegram_bot.py`)

Automated notifications at each stage:

```python
reporter = get_reporter()

# Task started
await reporter.notify_task_started(task_id, title, agent_id, branch, plan)

# Progress updates (every 5 min or phase completion)
await reporter.notify_progress(task_id, title, current_phase, total_phases, progress_pct)

# Validation results
await reporter.notify_validation_results(task_id, title, score, validation_results, accepted)

# Task complete
await reporter.notify_task_complete(task_id, title, score, duration_ms, tests_passed, tests_total, commits, files_changed, merge_commit)

# Task failed
await reporter.notify_task_failed(task_id, title, score, attempts, max_attempts, error)
```

### 6. Orchestrator (`godmode/orchestrator.py`)

Main daemon that coordinates the full lifecycle:

```python
orchestrator = GodModeOrchestrator(repo_root)

# Run forever (polls every 30s by default)
await orchestrator.run_forever(poll_interval=30)
```

**Execution flow:**
1. Claim next pending approved task (atomic with SKIP LOCKED)
2. Create isolated worktree
3. Send task started notification
4. Execute plan phases sequentially
5. Run validation pipeline
6. Self-heal if score < 70 (retry with feedback)
7. Merge to main on success
8. Send completion/failure notification
9. Cleanup worktree (preserve on failure for debugging)

### 7. Control Center UI (`godmode/api.py`)

FastAPI-based web dashboard:

- **Kanban board**: 7 columns (backlog → planning → pending → running → review → done → failed)
- **Real-time updates**: Server-Sent Events (SSE) stream
- **Task creation**: Web form with title, description, priority
- **Plan approval**: Review generated plan before execution
- **Task details**: Modal with validation results, progress, logs

**Routes:**
- `GET /godmode` - Dashboard HTML
- `GET /godmode/tasks` - List tasks (JSON)
- `POST /godmode/tasks` - Create task
- `GET /godmode/tasks/{id}` - Task details
- `POST /godmode/tasks/{id}/approve` - Approve plan
- `POST /godmode/tasks/{id}/cancel` - Cancel task
- `GET /godmode/events` - SSE stream

**Tech stack:**
- HTMX for dynamic updates
- Alpine.js for client-side interactivity
- Tailwind CSS for styling

## Usage

### 1. Initialize Database

```bash
psql -d jarvis -f scripts/init_godmode_db.sql
```

### 2. Start Orchestrator Daemon

```bash
# Standalone
python -m godmode.orchestrator

# Or integrate into KAIROS
# (Add to core/kairos.py background tasks)
```

### 3. Access Control Center

```bash
# Start FastAPI server (add to main.py)
# Then visit: http://localhost:8000/godmode
```

### 4. Create Task via API

```python
import httpx

async with httpx.AsyncClient() as client:
    response = await client.post(
        "http://localhost:8000/godmode/tasks",
        json={
            "title": "Implement feature X",
            "description": "Add new API endpoint for user preferences",
            "priority": 100
        }
    )
    task = response.json()["task"]
    print(f"Created task: {task['id']}")
```

### 5. Approve Plan

```python
# After coordinator generates plan, approve via UI or API
response = await client.post(
    f"http://localhost:8000/godmode/tasks/{task_id}/approve",
    json={"approved_by": "user"}
)
```

### 6. Monitor Progress

- **Web UI**: Real-time Kanban board updates via SSE
- **Telegram**: Notifications at each stage
- **Database**: Query `god_mode_tasks` and `god_mode_events` tables

## Configuration

Environment variables:

```bash
# Orchestrator
export GODMODE_POLL_INTERVAL=30  # Seconds between task polls

# Docker (optional)
export GODMODE_USE_DOCKER=true
export GODMODE_DOCKER_IMAGE=jarvis-godmode:latest
export GODMODE_CPU_LIMIT=2.0
export GODMODE_MEMORY_LIMIT=4g

# Telegram
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_USER_ID=your_user_id

# Validation
export GODMODE_MIN_SCORE=70  # Minimum passing score
export GODMODE_MAX_RETRIES=3  # Max self-healing attempts
```

## Testing

```bash
# Run integration tests
pytest tests/test_godmode_integration.py -v

# Run specific test
pytest tests/test_godmode_integration.py::test_worktree_lifecycle -v

# Skip slow tests
pytest tests/test_godmode_integration.py -v -m "not slow"

# Skip Docker tests (if Docker not available)
pytest tests/test_godmode_integration.py -v -k "not docker"
```

## File Structure

```
godmode/
├── __init__.py
├── orchestrator.py          # Main daemon
├── api.py                   # FastAPI Control Center
├── isolation/
│   ├── __init__.py
│   ├── worktree.py         # Git worktree isolation
│   └── docker.py           # Docker container sandboxing
├── validation/
│   ├── __init__.py
│   └── pipeline.py         # Quality scoring and self-healing
└── reporting/
    ├── __init__.py
    └── telegram_bot.py     # Telegram notifications

scripts/
└── init_godmode_db.sql     # Database schema

tests/
└── test_godmode_integration.py  # Integration tests
```

## Integration with JARVIS

To integrate God Mode into the main JARVIS system:

### 1. Add to FastAPI app (`main.py`)

```python
from godmode.api import router as godmode_router

app.include_router(godmode_router)
```

### 2. Add to KAIROS daemon (`core/kairos.py`)

```python
from godmode.orchestrator import GodModeOrchestrator

async def kairos_loop():
    orchestrator = GodModeOrchestrator(Path("/root/Ai-bot"))
    
    # Run orchestrator in background
    asyncio.create_task(orchestrator.run_forever(poll_interval=30))
    
    # ... rest of KAIROS loop
```

### 3. Add to systemd service

```ini
[Unit]
Description=JARVIS God Mode Orchestrator
After=network.target postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/Ai-bot
ExecStart=/usr/bin/python3 -m godmode.orchestrator
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Security Considerations

1. **Worktree isolation**: Each agent works in isolated git worktree, preventing conflicts
2. **Docker sandboxing**: Optional container isolation with resource limits
3. **Secret scanning**: Validation pipeline checks for hardcoded secrets
4. **Security zones**: Agents operate in green zone by default
5. **Approval workflow**: Plans require explicit approval before execution
6. **Audit logging**: All events logged to `god_mode_events` table

## Performance

- **Parallel execution**: Multiple agents can run concurrently (SKIP LOCKED)
- **Resource limits**: Docker containers capped at 2 CPU cores, 4GB RAM
- **Validation caching**: Syntax checks run only on changed files
- **SSE streaming**: Real-time UI updates without polling overhead

## Troubleshooting

### Task stuck in "running" status

```sql
-- Check agent logs
SELECT * FROM god_mode_events WHERE task_id = 'xxx' ORDER BY ts DESC;

-- Reset task to pending
UPDATE god_mode_tasks SET status = 'pending', agent_id = NULL WHERE id = 'xxx';
```

### Worktree not cleaned up

```bash
# List worktrees
git worktree list

# Remove manually
git worktree remove .claude/worktrees/task-xxx --force
git branch -D godmode/task-xxx
```

### Docker container still running

```bash
# List containers
docker ps -a | grep godmode

# Stop and remove
docker stop godmode-xxx
docker rm godmode-xxx
```

### Validation always failing

```bash
# Check validation results
SELECT validation_results FROM god_mode_tasks WHERE id = 'xxx';

# Lower threshold temporarily
export GODMODE_MIN_SCORE=50
```

## Future Enhancements

- [ ] Multi-repository support (orchestrate across multiple repos)
- [ ] Agent templates (pre-defined plans for common tasks)
- [ ] Performance benchmarking (track validation scores over time)
- [ ] Slack integration (in addition to Telegram)
- [ ] Web-based plan editor (modify plans before approval)
- [ ] Agent collaboration (agents can spawn sub-agents)
- [ ] Cost tracking (token usage per task)

## References

- Database schema: `scripts/init_godmode_db.sql`
- Integration tests: `tests/test_godmode_integration.py`
- JARVIS CLAUDE.md: Agent orchestration guidelines
- PostgreSQL SKIP LOCKED: https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE
