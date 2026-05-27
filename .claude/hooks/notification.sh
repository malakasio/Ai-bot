#!/usr/bin/env bash
# notification.sh — send alerts for critical events.
# Invoked by Claude Code CLI on critical errors, trips, or milestones.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOG_FILE="${LOG_FILE:-/tmp/jarvis-hooks.log}"
EVENT="${CLAUDE_NOTIFY_EVENT:-$1}"
MESSAGE="${CLAUDE_NOTIFY_MESSAGE:-${2:-}}"

log() {
    echo "[notification] $(date -u +%Y-%m-%dT%H:%M:%SZ) event=${EVENT} $*" >> "$LOG_FILE"
}

# ─── Telegram notification (if bot token configured) ────────────────────────
send_telegram() {
    local TELEGRAM_BOT_TOKEN TELEGRAM_USER_ID
    TELEGRAM_BOT_TOKEN=$(grep TELEGRAM_BOT_TOKEN "$REPO_ROOT/.env" 2>/dev/null | cut -d= -f2- || echo "")
    TELEGRAM_USER_ID=$(grep TELEGRAM_USER_ID "$REPO_ROOT/.env" 2>/dev/null | cut -d= -f2- || echo "")

    if [[ -n "$TELEGRAM_BOT_TOKEN" ]] && [[ -n "$TELEGRAM_USER_ID" ]]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_USER_ID}" \
            -d "text=🤖 J.A.R.V.I.S. | ${EVENT}: ${MESSAGE}" \
            -d "parse_mode=HTML" > /dev/null 2>&1 || true
        log "telegram sent"
    fi
}

case "${EVENT}" in
    critical|error|trip|milestone)
        log "${MESSAGE}"
        send_telegram
        ;;
    *)
        log "unknown event type: ${EVENT}"
        ;;
esac

exit 0
