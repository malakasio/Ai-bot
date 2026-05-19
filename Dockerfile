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
    JARVIS_HOME=/var/lib/jarvis

# System deps: git for snapshots, curl for healthchecks, postgresql-client
# for psql-based DB bootstrap, ca-certificates for outbound HTTPS.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git postgresql-client \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/jarvis

# Install Python deps first for layer caching.
COPY requirements.txt requirements-minimal.txt ./
RUN pip install --upgrade pip \
    && pip install \
        "fastapi>=0.115" "uvicorn[standard]>=0.32" \
        "anthropic>=0.40" "httpx>=0.27" "tenacity>=8" \
        "asyncpg>=0.29" "pgvector>=0.3" \
        "websockets>=12" \
        "python-dotenv>=1" \
        "pytest>=8" "pytest-asyncio>=0.23"

# Now copy the source.
COPY . /opt/jarvis

# Create runtime dirs that the app expects.
RUN mkdir -p /var/lib/jarvis /var/log/jarvis /tmp/jarvis \
    && chmod 700 /var/lib/jarvis /var/log/jarvis

EXPOSE 8000

# Docker HEALTHCHECK runs INSIDE the container, so it must hit whatever
# port uvicorn is listening on. PaaS platforms inject $PORT — Railway,
# Render, Fly — so we honor it here too.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/healthz" || exit 1

# Run via the integrated FastAPI app. main.py loads agent + sentinel
# + KAIROS + voice WS + observability dashboard via the lifespan hook.
# Shell form so ${PORT:-8000} expands at container start: PaaS
# platforms inject $PORT and we must listen on it or the health probe
# never connects.
CMD exec python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
