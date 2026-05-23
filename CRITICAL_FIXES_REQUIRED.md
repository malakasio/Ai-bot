# CRITICAL FIXES - IMMEDIATE ACTION REQUIRED
**Date:** 2026-05-23  
**Priority:** URGENT

---

## 🚨 SECURITY VULNERABILITIES - FIX NOW

### 1. SQL INJECTION - core/memory.py (CRITICAL)

**Location:** Lines 116-126  
**Risk:** Arbitrary SQL execution, data breach

**Current Code:**
```python
where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
rows = await database.fetch(
    f"""
    SELECT id, ts, actor, tool, input, output, exit_code, zone,
           score, duration_ms, failure_mode, task_id, metadata
    FROM history_logs
    {where}  # ← VULNERABLE
    ORDER BY ts DESC
    LIMIT ${len(args)}
    """,
    *args,
)
```

**Fix Required:**
```python
# Build parameterized query
params = []
param_idx = 1
where_parts = []

if actor:
    where_parts.append(f"actor = ${param_idx}")
    params.append(actor)
    param_idx += 1

if tool:
    where_parts.append(f"tool = ${param_idx}")
    params.append(tool)
    param_idx += 1

# ... continue for all filters

where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

rows = await database.fetch(
    f"""
    SELECT id, ts, actor, tool, input, output, exit_code, zone,
           score, duration_ms, failure_mode, task_id, metadata
    FROM history_logs
    {where}
    ORDER BY ts DESC
    LIMIT ${param_idx}
    """,
    *params, limit
)
```

---

### 2. COMMAND INJECTION - godmode/orchestrator.py (CRITICAL)

**Location:** Lines 485-491  
**Risk:** Arbitrary command execution, system compromise

**Current Code:**
```python
command = step.get("command")
# No validation!
proc = await asyncio.create_subprocess_shell(
    command,  # ← VULNERABLE
    cwd=worktree.path,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
```

**Fix Required:**
```python
command = step.get("command")

# Validate command
allowed_commands = ["git", "npm", "python", "pytest", "ruff", "mypy"]
first_word = command.split()[0] if command.split() else ""

if first_word not in allowed_commands:
    raise ValueError(f"Command not allowed: {first_word}")

# Use argument array instead of shell
import shlex
args = shlex.split(command)

proc = await asyncio.create_subprocess_exec(
    *args,  # ← SAFE
    cwd=worktree.path,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
```

---

### 3. RACE CONDITION - core/database.py (CRITICAL)

**Location:** Lines 78-94  
**Risk:** Multiple database pools, connection leaks

**Current Code:**
```python
async def init() -> "asyncpg.Pool":
    if asyncpg is None:
        raise RuntimeError(...)
    global _pool
    if _pool is None:  # ← Check BEFORE lock
        async with _pool_lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(...)
    return _pool
```

**Fix Required:**
```python
async def init() -> "asyncpg.Pool":
    if asyncpg is None:
        raise RuntimeError(...)
    global _pool
    async with _pool_lock:  # ← Lock FIRST
        if _pool is None:
            _pool = await asyncpg.create_pool(...)
    return _pool
```

---

### 4. TRANSACTION SAFETY - core/database.py (CRITICAL)

**Location:** Lines 162-170  
**Risk:** Inconsistent database state

**Current Code:**
```python
async def __aexit__(self, exc_type, exc, tb):
    try:
        if exc_type is None:
            await self._txn.commit()
        else:
            await self._txn.rollback()
    finally:
        if self._conn is not None and self._pool is not None:
            await self._pool.release(self._conn)  # ← Released even if commit/rollback failed
```

**Fix Required:**
```python
async def __aexit__(self, exc_type, exc, tb):
    try:
        if exc_type is None:
            await self._txn.commit()
        else:
            await self._txn.rollback()
    except Exception as e:
        # Ensure transaction is closed even on error
        try:
            await self._txn.rollback()
        except:
            pass
        raise e
    finally:
        if self._conn is not None and self._pool is not None:
            await self._pool.release(self._conn)
```

---

### 5. EXPOSED SECRETS - .env (CRITICAL)

**Location:** /root/Ai-bot/.env  
**Risk:** Credential theft, unauthorized access

**Current:**
```bash
TELEGRAM_BOT_TOKEN=7965507190:AAFt-3aADUXkXTYfSqTcg8O6syEC4T4NtgY
N8N_API_KEY=<267-char JWT token>
```

**Action Required:**
1. **IMMEDIATELY** remove these from .env
2. Regenerate both tokens
3. Use environment variables or systemd credentials
4. Add .env to .gitignore (already done)
5. Check git history for committed secrets

---

### 6. COMMAND INJECTION - core/sentinel.py (HIGH)

**Location:** Lines 508-509  
**Risk:** IP spoofing, iptables bypass

**Current Code:**
```python
rc, so, se = await _run([ipt, "-A", "INPUT", "-s", ip, "-j", "DROP"])
```

**Fix Required:**
```python
import ipaddress

# Validate IP format
try:
    ipaddress.ip_address(ip)
except ValueError:
    logger.error(f"Invalid IP address: {ip}")
    return

rc, so, se = await _run([ipt, "-A", "INPUT", "-s", ip, "-j", "DROP"])
```

---

### 7. PATH TRAVERSAL - mcp/filesystem_mcp.py (HIGH)

**Location:** Lines 68-92  
**Risk:** TOCTOU vulnerability, unauthorized file access

**Current Code:**
```python
resolved = p.resolve(strict=False)
# ... validation ...
# Time gap here - symlink could be created
path.write_bytes(blob)
```

**Fix Required:**
```python
resolved = p.resolve(strict=True)  # Fail if doesn't exist
# ... validation ...
# Re-validate immediately before write
if not _is_allowed(resolved):
    return error_response("path not allowed")
path.write_bytes(blob)
```

---

### 8. SSRF VULNERABILITY - mcp/network_mcp.py (HIGH)

**Location:** Lines 92-97  
**Risk:** DNS rebinding attack, internal network access

**Current Code:**
```python
if _is_private_or_loopback(host) and not _allow_private():
    return (...)
# DNS resolution happens at check time, but request happens later
```

**Fix Required:**
```python
# Resolve IP first
import socket
ip = socket.gethostbyname(host)

# Check resolved IP
if _is_private_or_loopback(ip) and not _allow_private():
    return (...)

# Use resolved IP for request
response = await session.get(f"http://{ip}{path}")
```

---

## 🔧 IMMEDIATE ACTIONS CHECKLIST

- [ ] **STOP:** Remove secrets from .env immediately
- [ ] **FIX:** SQL injection in core/memory.py
- [ ] **FIX:** Command injection in godmode/orchestrator.py
- [ ] **FIX:** Race condition in core/database.py
- [ ] **FIX:** Transaction safety in core/database.py
- [ ] **FIX:** IP validation in core/sentinel.py
- [ ] **FIX:** Path traversal in mcp/filesystem_mcp.py
- [ ] **FIX:** SSRF in mcp/network_mcp.py
- [ ] **TEST:** Run security test suite
- [ ] **VERIFY:** No secrets in git history
- [ ] **DEPLOY:** Only after all fixes verified

---

## 📞 ESCALATION

If you cannot fix these immediately:
1. **DISABLE** affected features (God Mode, network MCP)
2. **RESTRICT** database access to localhost only
3. **ROTATE** all exposed credentials
4. **NOTIFY** security team

---

**DO NOT DEPLOY TO PRODUCTION UNTIL THESE ARE FIXED**
