# God Mode Deployment Guide

## Quick Start (5 minutes)

### 1. Initialize Database

```bash
cd /root/Ai-bot
python3 scripts/init_godmode.py
```

Expected output:
```
[godmode-init] ✅ God Mode database initialization complete!
```

### 2. Restart JARVIS

```bash
systemctl restart jarvis
```

### 3. Verify Deployment

```bash
# Check service status
systemctl status jarvis

# Check logs
journalctl -u jarvis -f

# Look for these lines:
# [INFO] daemon.godmode.start
# [INFO] mount.godmode ok
```

### 4. Access Control Center

Open browser: `http://localhost:8000/godmode`

You should see the Kanban board with 7 columns:
- backlog → planning → pending → running → review → done → failed

### 5. Create Your First Task

**Via UI:**
1. Click "+ New Task" button
2. Fill in title, description, priority
3. Click "Create"

**Via API:**
```bash
curl -X POST http://localhost:8000/godmode/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Add user authentication",
    "description": "Implement JWT-based auth with refresh tokens",
    "priority": 100
  }'
```

**Via Python:**
```python
import httpx
import asyncio

async def create_task():
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/godmode/tasks",
            json={
                "title": "Add user authentication",
                "description": "Implement JWT-based auth",
                "priority": 100
            }
        )
        task = response.json()["task"]
        print(f"Created task: {task['id']}")
        return task

asyncio.run(create_task())
```

---

## Configuration

### Environment Variables

Add to `/root/Ai-bot/.env` or export in shell:

```bash
# God Mode Orchestrator
export GODMODE_ENABLED=true              # Enable/disable God Mode (default: true)
export GODMODE_POLL_INTERVAL=30          # Seconds between task polls (default: 30)
export GODMODE_MIN_SCORE=70              # Minimum validation score (default: 70)
export GODMODE_MAX_RETRIES=3             # Max self-healing attempts (default: 3)

# Docker Isolation (optional)
export GODMODE_USE_DOCKER=false          # Enable Docker sandboxing (default: false)
export GODMODE_DOCKER_IMAGE=jarvis-godmode:latest
export GODMODE_CPU_LIMIT=2.0             # CPU cores per container
export GODMODE_MEMORY_LIMIT=4g           # Memory per container

# Telegram Notifications
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_USER_ID=your_user_id

# Database (already configured in JARVIS)
export DATABASE_URL=postgresql://user:pass@localhost/jarvis
```

### Feature Flags

```bash
# Disable God Mode temporarily
export GODMODE_ENABLED=false
systemctl restart jarvis

# Enable Docker isolation
export GODMODE_USE_DOCKER=true
systemctl restart jarvis

# Adjust polling frequency (faster for testing)
export GODMODE_POLL_INTERVAL=10
systemctl restart jarvis
```

---

## Workflow

### Task Lifecycle

```
1. CREATE TASK
   ↓
2. GENERATE PLAN (coordinator agent)
   ↓
3. APPROVE PLAN (user via UI/API)
   ↓
4. CLAIM TASK (orchestrator daemon)
   ↓
5. CREATE WORKTREE (isolated git branch)
   ↓
6. EXECUTE PHASES (run commands in worktree)
   ↓
7. VALIDATE (syntax, tests, security, logs)
   ↓
8. SELF-HEAL (if score < 70, retry with feedback)
   ↓
9. MERGE TO MAIN (if score >= 70)
   ↓
10. CLEANUP WORKTREE
    ↓
11. NOTIFY COMPLETION (Telegram)
```

### Manual Task Creation

**Step 1: Create task**
```bash
curl -X POST http://localhost:8000/godmode/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Fix bug in login flow",
    "description": "Users cannot login with email containing + character",
    "priority": 50
  }'
```

**Step 2: Generate plan (manual for now)**

For now, you need to manually create a plan. In the future, a coordinator agent will generate this automatically.

```python
import asyncio
from core import database

async def add_plan():
    task_id = "your-task-id-here"
    
    plan = {
        "phases": [
            {
                "name": "Reproduce bug",
                "steps": [
                    {
                        "step": "Create test case",
                        "command": "echo 'def test_email_with_plus(): ...' >> tests/test_auth.py"
                    },
                    {
                        "step": "Run test to confirm failure",
                        "command": "pytest tests/test_auth.py::test_email_with_plus -v"
                    }
                ]
            },
            {
                "name": "Fix validation",
                "steps": [
                    {
                        "step": "Update email regex",
                        "command": "sed -i 's/EMAIL_REGEX = .*/EMAIL_REGEX = r\"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$\"/' core/auth.py"
                    }
                ]
            },
            {
                "name": "Verify fix",
                "steps": [
                    {
                        "step": "Run all auth tests",
                        "command": "pytest tests/test_auth.py -v"
                    }
                ]
            }
        ]
    }
    
    await database.init()
    await database.execute(
        """
        UPDATE god_mode_tasks
        SET plan = $2::jsonb,
            plan_generated_at = now(),
            status = 'pending'
        WHERE id = $1
        """,
        task_id,
        database.json.dumps(plan)
    )
    await database.close()
    print(f"Plan added to task {task_id}")

asyncio.run(add_plan())
```

**Step 3: Approve plan**
```bash
curl -X POST http://localhost:8000/godmode/tasks/{task_id}/approve \
  -H "Content-Type: application/json" \
  -d '{"approved_by": "user"}'
```

**Step 4: Monitor progress**

- **UI**: Watch Kanban board at http://localhost:8000/godmode
- **Telegram**: Receive notifications at each stage
- **Database**: Query directly

```sql
-- Check task status
SELECT id, title, status, progress_pct, validation_score
FROM god_mode_tasks
WHERE id = 'your-task-id';

-- View events
SELECT ts, event_type, actor, data
FROM god_mode_events
WHERE task_id = 'your-task-id'
ORDER BY ts DESC;
```

---

## Monitoring

### Health Checks

```bash
# Overall health
curl http://localhost:8000/healthz | jq

# Check God Mode daemon
curl http://localhost:8000/healthz | jq '.daemons.godmode'

# Expected output:
# {
#   "name": "godmode",
#   "running": true,
#   "restart_count": 0,
#   "last_error": null,
#   "started_at": 1716500000.0
# }
```

### Logs

```bash
# Follow JARVIS logs
journalctl -u jarvis -f

# Filter God Mode logs
journalctl -u jarvis -f | grep godmode

# Check for errors
journalctl -u jarvis --since "10 minutes ago" | grep -i error
```

### Database Queries

```sql
-- Active tasks
SELECT id, title, status, current_phase, total_phases, progress_pct
FROM god_mode_tasks
WHERE status = 'running'
ORDER BY started_at DESC;

-- Recent completions
SELECT id, title, validation_score, duration_ms, finished_at
FROM god_mode_tasks
WHERE status = 'done'
ORDER BY finished_at DESC
LIMIT 10;

-- Failed tasks
SELECT id, title, error, validation_score, validation_attempts
FROM god_mode_tasks
WHERE status = 'failed'
ORDER BY finished_at DESC;

-- Task statistics
SELECT
    status,
    COUNT(*) as count,
    AVG(validation_score) as avg_score,
    AVG(duration_ms) / 1000 as avg_duration_sec
FROM god_mode_tasks
WHERE deleted_at IS NULL
GROUP BY status;
```

---

## Troubleshooting

### God Mode daemon not starting

**Symptom:** `curl http://localhost:8000/healthz | jq '.daemons.godmode.running'` returns `false`

**Check logs:**
```bash
journalctl -u jarvis -f | grep godmode
```

**Common causes:**
1. Database not initialized: Run `python3 scripts/init_godmode.py`
2. Import error: Check Python path and dependencies
3. Permission error: Ensure JARVIS can write to `.claude/worktrees/`

**Fix:**
```bash
# Verify database
python3 scripts/init_godmode.py

# Check permissions
ls -la .claude/
mkdir -p .claude/worktrees
chmod 755 .claude/worktrees

# Restart
systemctl restart jarvis
```

### Task stuck in "running" status

**Symptom:** Task shows "running" but no progress for >10 minutes

**Diagnose:**
```sql
-- Check task details
SELECT * FROM god_mode_tasks WHERE id = 'task-id';

-- Check recent events
SELECT * FROM god_mode_events
WHERE task_id = 'task-id'
ORDER BY ts DESC
LIMIT 10;
```

**Fix:**
```sql
-- Reset task to pending (will be picked up again)
UPDATE god_mode_tasks
SET status = 'pending',
    agent_id = NULL,
    current_phase = 0,
    progress_pct = 0
WHERE id = 'task-id';
```

### Worktree not cleaned up

**Symptom:** `.claude/worktrees/task-xxx` still exists after task completion

**Check:**
```bash
git worktree list
```

**Fix:**
```bash
# Remove worktree
git worktree remove .claude/worktrees/task-xxx --force

# Delete branch
git branch -D godmode/task-xxx
```

### Validation always failing

**Symptom:** All tasks fail validation with score < 70

**Check validation results:**
```sql
SELECT validation_results
FROM god_mode_tasks
WHERE id = 'task-id';
```

**Common causes:**
1. Syntax checker (ruff) too strict
2. Tests not configured
3. Security scanner false positives

**Fix:**
```bash
# Lower threshold temporarily
export GODMODE_MIN_SCORE=50
systemctl restart jarvis

# Or disable specific checks in validation pipeline
# Edit godmode/validation/pipeline.py
```

### Docker container issues

**Symptom:** Tasks fail with Docker errors

**Check:**
```bash
# Verify Docker is running
docker info

# Check for orphaned containers
docker ps -a | grep godmode

# Clean up
docker rm -f $(docker ps -a -q --filter "name=godmode")
```

**Build image:**
```bash
cd /root/Ai-bot
docker build -t jarvis-godmode:latest -f godmode/Dockerfile .
```

---

## Testing

### Unit Tests

```bash
# Run all God Mode tests
pytest tests/test_godmode_integration.py -v

# Run specific test
pytest tests/test_godmode_integration.py::test_worktree_lifecycle -v

# Skip slow tests
pytest tests/test_godmode_integration.py -v -m "not slow"

# Skip Docker tests
pytest tests/test_godmode_integration.py -v -k "not docker"
```

### Manual Testing

**Test 1: Simple task**
```bash
# Create task
curl -X POST http://localhost:8000/godmode/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Create test file",
    "description": "Simple test",
    "priority": 100
  }'

# Add simple plan (via Python script)
# Approve plan
# Monitor completion
```

**Test 2: Validation pipeline**
```bash
# Create task that will fail validation
# (e.g., task that creates file with syntax errors)
# Verify self-healing kicks in
# Check feedback in database
```

**Test 3: Real-time updates**
```bash
# Open Control Center in browser
# Create task via API
# Watch Kanban board update in real-time via SSE
```

---

## Performance Tuning

### Faster polling (development)

```bash
export GODMODE_POLL_INTERVAL=10  # Check every 10 seconds
systemctl restart jarvis
```

### Parallel execution

God Mode supports parallel execution out of the box via `SELECT FOR UPDATE SKIP LOCKED`. To run multiple orchestrators:

```bash
# Terminal 1
python3 -m godmode.orchestrator

# Terminal 2
python3 -m godmode.orchestrator

# Both will claim tasks atomically without conflicts
```

### Resource limits

```bash
# Limit CPU per task
export GODMODE_CPU_LIMIT=1.0

# Limit memory per task
export GODMODE_MEMORY_LIMIT=2g

# Enable Docker isolation
export GODMODE_USE_DOCKER=true

systemctl restart jarvis
```

---

## Security

### Worktree Isolation

- Each task runs in isolated git worktree
- No shared state between tasks
- Changes only merged after validation passes

### Docker Sandboxing (optional)

- Read-only root filesystem
- No new privileges
- Resource limits enforced
- Network isolation available

### Secret Scanning

Validation pipeline automatically scans for:
- API keys
- Secret keys
- Passwords
- Tokens
- Private keys

Tasks with secrets in plaintext will fail validation.

### Security Zones

God Mode agents operate in **green zone** by default:
- Full read/write: `/root/Ai-bot/`, `/tmp/jarvis/`
- No access to: `/etc`, `/var`, `/usr`, system files

---

## Backup & Recovery

### Database Backup

```bash
# Backup God Mode tables
pg_dump -d jarvis \
  -t god_mode_tasks \
  -t god_mode_phases \
  -t god_mode_events \
  > godmode_backup_$(date +%Y%m%d).sql

# Restore
psql -d jarvis < godmode_backup_20260523.sql
```

### Worktree Recovery

If a task fails and worktree is preserved:

```bash
# List worktrees
git worktree list

# Inspect changes
cd .claude/worktrees/task-xxx
git log
git diff main

# Manually merge if needed
git checkout main
git merge godmode/task-xxx

# Or discard
git worktree remove .claude/worktrees/task-xxx --force
git branch -D godmode/task-xxx
```

---

## Next Steps

1. **Create your first task** via UI or API
2. **Monitor progress** in Control Center
3. **Review validation results** and adjust thresholds
4. **Enable Telegram notifications** for mobile updates
5. **Scale up** by running multiple orchestrators
6. **Integrate with CI/CD** via webhooks

For more details, see:
- `godmode/README.md` - Full architecture documentation
- `tests/test_godmode_integration.py` - Integration tests
- `scripts/init_godmode_db.sql` - Database schema

---

## Support

- **Issues**: https://github.com/malakasio/Ai-bot/issues
- **Logs**: `journalctl -u jarvis -f`
- **Database**: `psql -d jarvis`
- **Health**: http://localhost:8000/healthz
