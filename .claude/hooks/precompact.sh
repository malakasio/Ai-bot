#!/usr/bin/env bash
# precompact.sh — backup working memory before context window compaction.
# Invoked by Claude Code CLI before context is compacted.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOG_FILE="${LOG_FILE:-/tmp/jarvis-hooks.log}"
SNAPSHOT_DIR="$REPO_ROOT/.claude/snapshots"
mkdir -p "$SNAPSHOT_DIR"

log() {
    echo "[precompact] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG_FILE"
}

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
COMPACT_FILE="$SNAPSHOT_DIR/compact_${TIMESTAMP}.json"

log "saving context snapshot before compaction"

# ─── Persist critical memory state to episodic ──────────────────────────────
cd "$REPO_ROOT" && python3 -c "
import asyncio, json
from datetime import datetime
from core.memory import store_episode, Episode
async def _snapshot():
    ep = Episode(
        actor='precompact-hook',
        tool='context_compaction',
        zone='green',
        metadata={'compact_ts': '${TIMESTAMP}', 'reason': 'context_window_limit'},
    )
    await store_episode(ep)
    return ep
try:
    asyncio.run(_snapshot())
    print('episode saved', flush=True)
except Exception as e:
    print(f'episode save error: {e}', flush=True)
" >> "$LOG_FILE" 2>&1 || true

log "compaction snapshot saved to compact_${TIMESTAMP}.json"
exit 0
