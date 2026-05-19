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

```
                ┌──────────────────────────────────────────────┐
                │                 Operator                     │
                │     (voice · telegram · http · dashboard)    │
                └────────────────────┬─────────────────────────┘
                                     │
                          ┌──────────▼──────────┐
                          │       core/         │   ← orchestrator, router,
                          │  identity & policy  │     zone validator, audit
                          └─┬────────┬────────┬─┘
                            │        │        │
                  ┌─────────▼─┐  ┌───▼───┐  ┌─▼─────────┐
                  │   mcp/    │  │skills/│  │ memory/   │
                  │  servers  │  │ how-to│  │ pg+vector │
                  └───────────┘  └───────┘  └───────────┘
                            │
                  ┌─────────▼──────────┐
                  │     scripts/       │   ← bash-only filesystem ops
                  └────────────────────┘
```

### Top-level layout

| Path | Purpose |
|------|---------|
| `core/` | Orchestrator, model router, zone validator, audit logger, snapshot manager. The trust-critical code. |
| `mcp/` | Model Context Protocol servers and clients. Tool surface for the LLM. |
| `skills/` | One subdirectory per skill, each with a `SKILL.md` (procedural memory). |
| `config/` | Static configuration (zones, model routing, MCP wiring). Not secrets. |
| `scripts/` | Bash entry points for every filesystem-touching operation. P4 lives here. |
| `tests/` | Unit and integration tests, including protocol-compliance tests. |
| `CLAUDE.md` | Behavioral contract. The DNA. |
| `.env.example` | Environment variables — copy to `.env` and fill in. |

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

```bash
# 1. Copy env and fill in secrets (Telegram token, DB password, API keys)
cp .env.example .env
$EDITOR .env

# 2. Bootstrap dependencies (Postgres + pgvector + Python deps)
./scripts/setup.sh

# 3. Initialize the database schema
./scripts/db-init.sh

# 4. Start JARVIS
./scripts/jarvis start
```

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

## Model routing

| Task class | Model |
|------------|-------|
| Conversation, STT/TTS routing | Claude Haiku |
| File ops, code review, logs | Claude Sonnet |
| Architecture, deep debugging | Claude Opus |
| Simple offline tasks | Local LLM via Ollama |

Routing decisions are logged. The router is in `core/router.py`.

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
