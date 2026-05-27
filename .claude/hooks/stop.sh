#!/usr/bin/env bash
# stop.sh — clean shutdown for J.A.R.V.I.S.
# Invoked by Claude Code CLI on session exit.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOG_FILE="${LOG_FILE:-/tmp/jarvis-hooks.log}"

log() {
    echo "[stop] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG_FILE"
}

log "shutdown initiated"

# ─── Close database pool ────────────────────────────────────────────────────
cd "$REPO_ROOT" && python3 -c "
import asyncio
from core import database
try:
    asyncio.run(database.close())
    print('database pool closed', flush=True)
except Exception as e:
    print(f'database close error: {e}', flush=True)
" >> "$LOG_FILE" 2>&1 || true

# ─── Stop LiteLLM proxy ─────────────────────────────────────────────────────
if pgrep -f "litellm.*4000" > /dev/null; then
    pkill -f "litellm.*4000" 2>/dev/null || true
    log "liteLLM proxy stopped"
fi

# ─── Persist working memory snapshot ────────────────────────────────────────
SNAPSHOT_DIR="$REPO_ROOT/.claude/snapshots"
mkdir -p "$SNAPSHOT_DIR"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
echo "{\"shutdown_at\": \"$TIMESTAMP\", \"exit_code\": ${CLAUDE_EXIT_CODE:-0}}" \
    > "$SNAPSHOT_DIR/shutdown_${TIMESTAMP}.json"
log "snapshot saved to shutdown_${TIMESTAMP}.json"

log "shutdown complete"
exit 0
