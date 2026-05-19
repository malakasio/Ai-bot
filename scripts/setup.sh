#!/usr/bin/env bash
# JARVIS v7.0 setup.sh
#
# Two profiles:
#   --minimal  Just enough to run the orchestrator, agent loop, and the
#              MCP filesystem/network servers. No Postgres, no voice
#              dependencies. Suitable for laptops and CI.
#   --full     Everything: Postgres + pgvector schema, voice stack,
#              optional Ollama, systemd units (if running as root).
#
# Default if neither flag is given: --minimal.
#
# Idempotent: safe to re-run. Side-effects per step are guarded by
# "is it already done" checks.
#
# Usage:
#   ./scripts/setup.sh [--minimal|--full] [--no-pip] [--no-db]
#                      [--with-ollama] [--with-systemd]

set -euo pipefail

PROFILE="minimal"
DO_PIP=true
DO_DB=true
WITH_OLLAMA=false
WITH_SYSTEMD=false

for arg in "$@"; do
  case "$arg" in
    --minimal)        PROFILE="minimal" ;;
    --full)           PROFILE="full" ;;
    --no-pip)         DO_PIP=false ;;
    --no-db)          DO_DB=false ;;
    --with-ollama)    WITH_OLLAMA=true ;;
    --with-systemd)   WITH_SYSTEMD=true ;;
    -h|--help)
      sed -n '2,22p' "$0"; exit 0 ;;
    *)
      echo "setup: unknown arg: $arg" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "$0")"/.. && pwd)"
cd "${HERE}"

say() { printf '\n=== %s ===\n' "$*"; }

# ─── Sanity: python, git ──────────────────────────────────────────────────

say "Checking python and git"
command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
PYTHON="${PYTHON:-python3}"
if ! "${PYTHON}" -c "import sys; assert sys.version_info >= (3, 10)" >/dev/null 2>&1; then
  echo "python >= 3.10 required (got $("${PYTHON}" -V 2>&1))" >&2
  exit 1
fi
"${PYTHON}" -V

# ─── Pip dependencies ─────────────────────────────────────────────────────

if [ "${DO_PIP}" = "true" ]; then
  if [ "${PROFILE}" = "minimal" ]; then
    REQS=("anthropic" "httpx" "tenacity")
  else
    REQS=(
      "anthropic" "httpx" "tenacity"
      "asyncpg" "pgvector"
      "fastapi" "uvicorn[standard]" "websockets"
    )
  fi
  say "Installing pip deps (${PROFILE}): ${REQS[*]}"
  PIP_FLAGS=(--upgrade --quiet)
  if [ "$(id -u)" -eq 0 ]; then
    PIP_FLAGS+=(--break-system-packages)
  fi
  "${PYTHON}" -m pip install "${PIP_FLAGS[@]}" "${REQS[@]}"
fi

# ─── Database (full only) ─────────────────────────────────────────────────

if [ "${PROFILE}" = "full" ] && [ "${DO_DB}" = "true" ]; then
  say "Bootstrapping PostgreSQL schema"
  if ! command -v psql >/dev/null; then
    echo "psql not found; skipping (install postgresql-client to enable)" >&2
  else
    PG_HOST="${POSTGRES_HOST:-localhost}"
    PG_PORT="${POSTGRES_PORT:-5432}"
    PG_DB="${POSTGRES_DB:-jarvis}"
    PG_USER="${POSTGRES_USER:-jarvis}"
    if [ -n "${POSTGRES_PASSWORD:-}" ]; then
      export PGPASSWORD="${POSTGRES_PASSWORD}"
    fi
    echo "applying scripts/init_db.sql to ${PG_USER}@${PG_HOST}:${PG_PORT}/${PG_DB}"
    if psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}" \
            -v ON_ERROR_STOP=1 -f scripts/init_db.sql; then
      echo "schema applied"
    else
      echo "schema apply failed (continuing; fix and re-run)" >&2
    fi
  fi
fi

# ─── Ollama (optional) ────────────────────────────────────────────────────

if [ "${WITH_OLLAMA}" = "true" ]; then
  say "Installing Ollama"
  if command -v ollama >/dev/null; then
    echo "ollama already installed: $(ollama --version 2>&1 | head -1)"
  else
    if command -v curl >/dev/null; then
      curl -fsSL https://ollama.com/install.sh | sh
    else
      echo "curl missing; install Ollama manually" >&2
    fi
  fi
fi

# ─── systemd units (optional, requires root) ──────────────────────────────

if [ "${WITH_SYSTEMD}" = "true" ]; then
  say "Installing systemd units"
  if [ "$(id -u)" -ne 0 ]; then
    echo "skipping: --with-systemd needs root" >&2
  else
    UNIT_DIR="/etc/systemd/system"
    cat > "${UNIT_DIR}/jarvis_core.service" <<UNIT
[Unit]
Description=JARVIS core agent
After=network.target

[Service]
Type=simple
ExecStart=$(command -v python3) -m core.agent
WorkingDirectory=${HERE}
EnvironmentFile=-${HERE}/.env
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
    cat > "${UNIT_DIR}/jarvis_kairos.service" <<UNIT
[Unit]
Description=JARVIS KAIROS daemon
After=network.target jarvis_core.service

[Service]
Type=simple
ExecStart=$(command -v python3) -m core.kairos
WorkingDirectory=${HERE}
EnvironmentFile=-${HERE}/.env
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
    cat > "${UNIT_DIR}/jarvis_sentinel.service" <<UNIT
[Unit]
Description=JARVIS Red Zone Sentinel
After=network.target

[Service]
Type=simple
ExecStart=$(command -v python3) -m core.sentinel
WorkingDirectory=${HERE}
EnvironmentFile=-${HERE}/.env
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    echo "installed: jarvis_core.service, jarvis_kairos.service, jarvis_sentinel.service"
    echo "enable with: systemctl enable --now jarvis_core jarvis_kairos jarvis_sentinel"
  fi
fi

# ─── chmod the new scripts ────────────────────────────────────────────────

say "Making scripts executable"
chmod +x scripts/*.sh 2>/dev/null || true

# ─── Final dep audit ──────────────────────────────────────────────────────

say "Dependency audit"
"${PYTHON}" scripts/check_deps.py || true

say "setup ${PROFILE} done"
