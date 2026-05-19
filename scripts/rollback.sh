#!/usr/bin/env bash
# JARVIS v7.0 rollback.sh — undo the most recent commit, restore stashed
# state if any, and restart the agent.
#
# Usage:
#   ./scripts/rollback.sh                 # roll back HEAD by 1 commit
#   ./scripts/rollback.sh <tag-or-ref>    # roll back to a specific tag
#   ./scripts/rollback.sh --dry-run       # show what would happen
#   ./scripts/rollback.sh --no-restart    # skip systemctl restart
#
# Behavior:
#   1. Refuses if you're not in a git repo or HEAD is unset.
#   2. If a target ref is given, validates it exists, then
#      `git reset --hard <ref>`. Otherwise `git reset --hard HEAD~1`.
#   3. If there's a stash at refs/stash, attempts `git stash pop`. A
#      conflict on pop is logged but does not abort the script.
#   4. Restarts the systemd unit named by JARVIS_SERVICE (default
#      `jarvis_core`) unless --no-restart was passed or systemctl is
#      missing or we're not root.
#
# Exit codes:
#   0  ok
#   1  not a git repo
#   2  HEAD invalid or only one commit (cannot HEAD~1)
#   3  target ref does not exist
#   4  reset failed
#   5  service restart failed

set -euo pipefail

TARGET=""
DRY_RUN=false
DO_RESTART=true

for arg in "$@"; do
  case "$arg" in
    --dry-run)     DRY_RUN=true ;;
    --no-restart)  DO_RESTART=false ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *)
      if [ -z "${TARGET}" ]; then TARGET="$arg"; fi ;;
  esac
done

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "rollback: not a git repository" >&2
  exit 1
fi
if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
  echo "rollback: HEAD is not set" >&2
  exit 2
fi

if [ -n "${TARGET}" ]; then
  if ! git rev-parse --verify "${TARGET}^{commit}" >/dev/null 2>&1; then
    echo "rollback: target ref not found: ${TARGET}" >&2
    exit 3
  fi
  RESET_REF="${TARGET}"
else
  if ! git rev-parse --verify HEAD~1 >/dev/null 2>&1; then
    echo "rollback: cannot HEAD~1 (only one commit on this branch)" >&2
    exit 2
  fi
  RESET_REF="HEAD~1"
fi

BEFORE_SHA="$(git rev-parse HEAD)"
TARGET_SHA="$(git rev-parse "${RESET_REF}")"

echo "rollback: HEAD ${BEFORE_SHA} -> ${TARGET_SHA} (${RESET_REF})" >&2

if [ "${DRY_RUN}" = "true" ]; then
  echo "rollback: dry-run; no changes made" >&2
  exit 0
fi

# Safety net: tag the current HEAD so the rollback itself is undoable.
SAFETY_TAG="rolled-back-from/$(date -u +%Y%m%dT%H%M%SZ)"
git tag -a "${SAFETY_TAG}" -m "pre-rollback HEAD ${BEFORE_SHA}" HEAD || true

if ! git reset --hard "${RESET_REF}"; then
  echo "rollback: git reset failed" >&2
  exit 4
fi

# Restore any stash that snapshot.sh / pre-mutation work might have left.
if git rev-parse --verify refs/stash >/dev/null 2>&1; then
  if ! git stash pop; then
    echo "rollback: git stash pop reported conflicts; leaving stash in place" >&2
  fi
fi

# Restart the service if asked and we can.
if [ "${DO_RESTART}" = "true" ]; then
  SVC="${JARVIS_SERVICE:-jarvis_core}"
  if command -v systemctl >/dev/null 2>&1; then
    if [ "$(id -u)" -ne 0 ]; then
      echo "rollback: not root; skipping systemctl restart of ${SVC}" >&2
    else
      if ! systemctl restart "${SVC}"; then
        echo "rollback: systemctl restart ${SVC} failed" >&2
        exit 5
      fi
      echo "rollback: restarted ${SVC}" >&2
    fi
  else
    echo "rollback: systemctl not present; skipping restart" >&2
  fi
fi

echo "rollback: ok"
