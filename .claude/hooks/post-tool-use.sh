#!/usr/bin/env bash
# post-tool-use.sh — log episode after tool execution.
# Invoked by Claude Code CLI after every tool call completes.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOG_FILE="${LOG_FILE:-/tmp/jarvis-hooks.log}"
TOOL_NAME="${CLAUDE_TOOL_NAME:-$1}"
EXIT_CODE="${CLAUDE_TOOL_EXIT_CODE:-${2:-0}}"
DURATION_MS="${CLAUDE_TOOL_DURATION_MS:-0}"

log() {
    echo "[post-tool-use] $(date -u +%Y-%m-%dT%H:%M:%SZ) tool=${TOOL_NAME} exit=${EXIT_CODE} dur=${DURATION_MS}ms $*" >> "$LOG_FILE"
}

# ─── Emit structured log entry ──────────────────────────────────────────────
if [[ "$EXIT_CODE" -ne 0 ]]; then
    log "FAILED"
else
    log "SUCCESS"
fi

# ─── Trigger autoDream if idle ──────────────────────────────────────────────
IDLE_FILE="/tmp/jarvis_last_action"
NOW=$(date +%s)
if [[ -f "$IDLE_FILE" ]]; then
    LAST=$(cat "$IDLE_FILE")
    DIFF=$((NOW - LAST))
    if [[ $DIFF -gt 900 ]]; then
        log "triggering autoDream (idle ${DIFF}s)"
        cd "$REPO_ROOT" && python3 -c "
import asyncio
from core.memory import dream_archive
asyncio.run(dream_archive())
" >> "$LOG_FILE" 2>&1 || true
    fi
fi
echo "$NOW" > "$IDLE_FILE"

exit 0
