# JARVIS v7.0

> Just A Rather Very Intelligent System — a persistent, self-improving,
> security-zoned digital assistant. Not a chatbot. A daemon with a brain.

JARVIS v7.0 is a ground-up rebuild on top of the v6.0 stack. The headline
changes are **hardcore protocol enforcement**, **bash-only filesystem
operations**, **MCP-based tool integration**, and **snapshot-before-mutate**
as a system-wide invariant.

For the binding behavioral contract — protocols, zones, snapshot rules — see
[`CLAUDE.md`](./CLAUDE.md). That file is the source of truth. This README is
a map.

---

## Architecture

Flat structure inside `core/`:

**Core modules:**
- `core/agent.py` — Main agent loop with Anthropic client, circuit breaker, MCP routing, metrics collection
- `core/kairos.py` — Background daemon (task queue, GitHub polling, autoDream)
- `core/sentinel.py` — Security daemon (red-zone monitoring, SSH brute-force detection)
- `core/telegram_bot.py` — Telegram integration with screenshot support
- `core/memory.py` — Episodic/semantic memory API over PostgreSQL
- `core/database.py` — PostgreSQL connection pool (asyncpg)

**New in v7.0 (2026-05-23):**
- `core/embeddings.py` — Multi-provider embeddings (sentence-transformers, Ollama, OpenAI, Jina AI) for semantic search with pgvector
- `core/metrics.py` — Prometheus-compatible observability (LLM tokens, costs, latency, voice metrics, circuit breaker trips)
- `core/llm_router.py` — Task-aware model selection with 12 task types, routing across Anthropic (Claude Haiku/Sonnet/Opus), Groq (free cloud), and Ollama (local)
- `core/orchestrator.py` — Hierarchical sub-agent coordination with quality scoring (rejects outputs < 70/100: "do not rubber-stamp weak work")

### How it's wired

`main.py` is the single entrypoint — a FastAPI app whose lifespan hook:

1. Loads `config/mcp_config.json`, instantiates each MCP server, and
   registers every tool with `core.agent` so the agent loop can call
   them.
2. Starts the **KAIROS** daemon (`core.kairos.Kairos`) as a supervised
   asyncio task. KAIROS drains the task queue, polls GitHub, runs
   health checks, and triggers `autoDream` after `DREAM_IDLE_THRESHOLD`
   seconds of idleness.
3. Starts the **Red-Zone Sentinel** (`core.sentinel.RedZoneSentinel`) as
   a supervised asyncio task. It tails `/var/log/auth.log` and hashes
   `/etc/passwd` / `/etc/ssh/sshd_config` for change detection.
4. Mounts the **voice WebSocket** server (`voice/voice`) and the
   **observability dashboard** (`/obs`).

If any single daemon crashes, the in-process supervisor restarts it
with exponential backoff (2 s → 60 s). systemd is the outer ring; the
in-process supervisor is the inner one.

### Top-level layout

| Path | Purpose |
|------|---------|
| `core/` | Agent loop, orchestrator, model router, zone validator, audit logger, snapshot manager, metrics, embeddings. The trust-critical code. |
| `mcp/` | Model Context Protocol servers and clients. Tool surface for the LLM. |
| `skills/` | One subdirectory per skill, each with a `SKILL.md` (procedural memory). |
| `config/` | Static configuration (zones, model routing, MCP wiring). Not secrets. |
| `scripts/` | Bash entry points for every filesystem-touching operation. P4 lives here. |
| `tests/` | Unit and integration tests, including protocol-compliance tests. |
| `CLAUDE.md` | Behavioral contract. The DNA. |
| `.env.example` | Environment variables — copy to `.env` and fill in. |

---

## Features

### Core Capabilities
- **Multi-agent orchestration** — Hierarchical sub-agent coordination with quality scoring (correctness, completeness, efficiency, safety). Rejects outputs < 70/100.
- **Semantic memory** — pgvector embeddings with multi-provider support (sentence-transformers local, Ollama, OpenAI, Jina AI free tier)
- **Task-aware LLM routing** — 12 task types (simple_qa, voice, code_review, architecture, etc.) automatically routed to optimal model tier
- **Real-time observability** — Prometheus metrics export at `/metrics` (LLM tokens, costs, latency, voice pipeline, circuit breaker)
- **Voice pipeline** — WebSocket-based with <500ms latency target (Deepgram STT → Claude → ElevenLabs TTS)
- **Browser automation** — Playwright with stealth mode, screenshot capture, networkidle wait strategy
- **Security zones** — Green/yellow/red/black path classification with pre-execution validation
- **Circuit breaker** — Auto-recovery from API failures with exponential backoff
- **Snapshot-before-mutate** — Git-based rollback points for all destructive operations

---

## The seven hardcore protocols

JARVIS v7.0 ships with seven protocols that are enforced in code, not in
prompts. They are restated here for visibility; the canonical version is in
[`CLAUDE.md`](./CLAUDE.md).

1. **P1 No fabrication** — tool outputs are never invented.
2. **P2 No plaintext secrets** — pre-commit scan, abort on match.
3. **P3 Snapshot before mutation** — `git stash` or `pre-mutation/*` tag.
4. **P4 Bash-only file ops** — every write/delete shells out via `scripts/`.
5. **P5 Audit every action** — structured append-only log.
6. **P6 Validate before execute** — zone validator gates every command.
7. **P7 Fail loud, fail fast** — no silent retries on destructive ops.

---

## Security zones

| Zone | Paths | Behavior |
|------|-------|----------|
| Green | `~/jarvis/`, project root, `/tmp/jarvis/`, `~/.cache/jarvis/` | read/write, no confirmation |
| Yellow | `~/`, `/tmp/`, `~/Documents`, etc. | read free, write needs confirm |
| Red | `/etc`, `/var`, `/usr`, `/opt`, `/system`, `/data` | blocked unless `JARVIS_ZONE=red` |
| Black | block devices, `/proc/*/mem`, private keys, wallets | never |

`JARVIS_LAB_MODE=true` only relaxes the yellow zone. Red and black require
explicit per-invocation opt-in.

---

## Quickstart

There are two supported deployment shapes.

### 1) Docker Compose (recommended for first run)

Brings up PostgreSQL + pgvector, n8n, and (optionally) JARVIS itself in
one shot. The `app` profile builds and runs the main container from the
local `Dockerfile`.

```bash
cp .env.example .env
$EDITOR .env                                  # fill ANTHROPIC_API_KEY, etc.

# Data plane only (Postgres + n8n) — JARVIS runs on the host:
docker-compose up -d postgres n8n

# Or everything in containers:
docker-compose --profile app up -d --build

# Tail logs:
docker-compose logs -f jarvis
```

Health check:

```bash
curl -fsS http://localhost:8000/healthz | jq
curl -fsS http://localhost:8000/readyz  | jq
```

### 2) Native + systemd (production host)

```bash
cp .env.example .env
sudo install -d -o jarvis -g jarvis /opt/jarvis /var/log/jarvis /var/lib/jarvis
sudo rsync -a --exclude=.git ./ /opt/jarvis/
sudo -u jarvis python3 -m venv /opt/jarvis/.venv
sudo -u jarvis /opt/jarvis/.venv/bin/pip install -r requirements.txt \
    fastapi "uvicorn[standard]" anthropic asyncpg pgvector httpx tenacity websockets

# Initialize the database schema:
psql -h localhost -U jarvis -d jarvis -f scripts/init_db.sql

# Install secrets (mode 0400, root:root) under /etc/jarvis/secrets/
sudo install -d -m 0700 /etc/jarvis/secrets
printf '%s' "$ANTHROPIC_API_KEY"   | sudo tee /etc/jarvis/secrets/anthropic_api_key >/dev/null
printf '%s' "$TELEGRAM_BOT_TOKEN"  | sudo tee /etc/jarvis/secrets/telegram_bot_token >/dev/null
sudo chmod 0400 /etc/jarvis/secrets/*

# Install systemd units (Restart=always, RestartSec=2s):
sudo install -m 0644 config/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jarvis_core jarvis_sentinel jarvis_kairos
sudo systemctl status jarvis_core
```

### Endpoints

Once running on `http://<host>:8000`:

| Path                     | Purpose                                            |
|--------------------------|----------------------------------------------------|
| `/`                      | Operator landing page                              |
| `/healthz`               | Liveness + per-daemon status                       |
| `/readyz`                | Readiness — fails if DB or any daemon is down      |
| `/metrics`               | Prometheus metrics (LLM tokens, costs, latency)    |
| `/agent/run` (POST)      | One agent turn — `{"prompt": "..."}`               |
| `/mcp/tools`             | List MCP tools                                     |
| `/mcp/call` (POST)       | Dispatch an MCP tool — `{"tool":"...","args":{}}`  |
| `/voice/voice` (WS)      | Voice pipeline (Deepgram → Claude → ElevenLabs)    |
| `/obs/`                  | Real-time observability dashboard                  |

### Tests

```bash
python3 -m pytest tests/ -v
```

The integration suite (`tests/test_integration_v7.py`) verifies that
`main.py` boots, MCP tools register into the agent, and the sentinel /
KAIROS daemons are constructable in dry-run.

The first run creates `~/.local/share/jarvis/` (audit log, snapshots) and
`/tmp/jarvis/` (MCP sockets, scratch).

---

## Memory architecture

Four tiers, increasing in persistence and latency:

- **Working** — in-context, current session, ~100k tokens.
- **Episodic** — PostgreSQL hypertables. *What happened, when, outcome.*
- **Semantic** — pgvector embeddings. *Facts, concepts, relationships.*
- **Procedural** — PostgreSQL `rules` + `skills/*/SKILL.md`. *How to do things.*

`autoDream` runs during idle windows and promotes episodic → semantic →
procedural. See `core/dream.py`.

---

## Model

JARVIS v7.0 uses **task-aware routing** via `core/llm_router.py`:

- **Simple tasks** (voice, notifications, simple Q&A) → Claude Haiku 4.5 (fast, cheap)
- **Code & analysis** (code review, generation, analysis) → Claude Sonnet 4.6 (balanced)
- **Complex tasks** (architecture, deep debug, critical) → Claude Opus 4.7 (maximum capability)

**Multi-provider support:**
- **Anthropic** (Claude) — primary, if `ANTHROPIC_API_KEY` set
- **Groq** (free cloud) — fallback, if `GROQ_API_KEY` set
- **Ollama** (local) — final fallback, always available

Override with `JARVIS_AGENT_MODEL` env var to force a specific model. The voice pipeline defaults to Haiku for its sub-500ms TTFT requirement.

---

## Self-improvement loop

After every significant task JARVIS scores itself 0–100 and, on `score < 70`,
appends a lesson to the relevant `skills/<skill>/SKILL.md`. A skill with a
rolling failure rate above 20% (window of 20) is flagged for review.

---

## Repository conventions

- **Commits** include `Co-Authored-By: JARVIS <jarvis@local>` when JARVIS
  authors the change.
- **No force-push** to shared branches.
- **No `--no-verify`** on commits.
- **No `git config --global`** without operator approval.
- Pre-commit hook scans for secret patterns; the hook is part of the safety
  net, not a suggestion.

---

## Legacy

The pre-v7.0 codebase lives under [`_legacy/`](./_legacy) for reference. It
is not built, tested, or imported by v7.0 code. Lift code from it
deliberately, not by habit.

---

## License

TBD.
