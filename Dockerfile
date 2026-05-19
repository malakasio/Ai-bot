# JARVIS v7.0 — main app container
#
# Builds on python:3.11-slim. Installs core dependencies needed by the
# v7.0 surface: FastAPI + uvicorn, anthropic SDK, asyncpg + pgvector,
# httpx, tenacity, websockets, opentelemetry (optional).
#
# Run with docker-compose (profile `app`):
#   docker-compose --profile app up -d
#
# Or standalone:
#   docker build -t jarvis:v7.0 .
#   docker run --rm -p 8000:8000 \
#       -e ANTHROPIC_API_KEY=... \
#       -e DATABASE_URL=postgresql://jarvis:jarvis@host.docker.internal:5432/jarvis \
#       jarvis:v7.0

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    JARVIS_HOME=/var/lib/jarvis \
    PORT=8000

# Minimal system deps. curl is required by HEALTHCHECK; ca-certificates
# for outbound HTTPS. We deliberately do NOT install build-essential
# or postgresql-client here — the deploy image stays lean so the build
# finishes inside Railway/Render free-tier limits.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/jarvis

# Install Python deps first for layer caching. requirements-deploy.txt
# is the minimum-viable set: FastAPI, uvicorn, anthropic, httpx,
# tenacity, websockets, python-dotenv. Heavier deps (faster-whisper,
# sentence-transformers, spaCy, etc.) are intentionally NOT installed
# in the deploy image — see requirements.txt for the full optional set.
COPY requirements-deploy.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements-deploy.txt

# Now copy the source.
COPY . /opt/jarvis

# Create runtime dirs that the app expects. Use /tmp paths so the
# container can run under restrictive PaaS UIDs that can't write to
# /var/lib.
RUN mkdir -p /var/lib/jarvis /var/log/jarvis /tmp/jarvis \
    && chmod 0777 /var/lib/jarvis /var/log/jarvis /tmp/jarvis || true

EXPOSE 8000

# Docker HEALTHCHECK runs INSIDE the container, so it must hit whatever
# port uvicorn is listening on. PaaS platforms inject $PORT — Railway,
# Render, Fly — so we honor it here too.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/healthz" || exit 1

# Shell form so ${PORT:-8000} expands at container start. PaaS
# platforms inject $PORT and we must listen on it or the upstream
# health probe never connects.
CMD exec python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
