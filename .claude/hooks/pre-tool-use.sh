#!/usr/bin/env bash
# pre-tool-use.sh — validate tool calls before execution.
# Invoked by Claude Code CLI before every tool call.
# Exit 0 = allow, exit 1 = block.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOG_FILE="${LOG_FILE:-/tmp/jarvis-hooks.log}"
TOOL_NAME="${CLAUDE_TOOL_NAME:-$1}"
TOOL_ARGS="${CLAUDE_TOOL_ARGS:-$2}"
SECURITY_ZONE="${JARVIS_ZONE:-standard}"

log() {
    echo "[pre-tool-use] $(date -u +%Y-%m-%dT%H:%M:%SZ) zone=${SECURITY_ZONE} tool=${TOOL_NAME} $*" >> "$LOG_FILE"
}

# ─── Block-list: tools that must never run ───────────────────────────────────
BLOCK_LIST=(
    "rm -rf /"
    "mkfs"
    "dd if="
    ":(){ :|:& };:"   # fork bomb
    "chmod 777 /"
    "> /dev/sda"
)

for pattern in "${BLOCK_LIST[@]}"; do
    if [[ "${TOOL_ARGS:-}" == *"$pattern"* ]]; then
        log "BLOCKED: matched deny-list pattern '${pattern}'"
        echo "::error::pre-tool-use: blocked by deny-list pattern '${pattern}'"
        exit 1
    fi
done

# ─── Red zone: only allowed when JARVIS_ZONE=red ────────────────────────────
RED_PATHS=("/etc" "/var" "/usr" "/opt" "/boot")
if [[ "${TOOL_NAME:-}" == *"file"* ]] || [[ "${TOOL_NAME:-}" == "bash" ]]; then
    if [[ "$SECURITY_ZONE" != "red" ]]; then
        for rp in "${RED_PATHS[@]}"; do
            if [[ "${TOOL_ARGS:-}" == *"$rp"* ]]; then
                log "BLOCKED: red zone path '${rp}' (zone=${SECURITY_ZONE})"
                echo "::error::pre-tool-use: path '${rp}' requires JARVIS_ZONE=red"
                exit 1
            fi
        done
    fi
fi

log "ALLOWED"
exit 0
