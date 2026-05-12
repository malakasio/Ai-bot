## Cursor Cloud Agent Instructions

### Project overview

JARVIS v6.0 is a Python 3.11 FastAPI autonomous assistant. See `README.md` for full architecture.

### Quick reference

| Action | Command |
|--------|---------|
| Run API (dev) | `PYTHONPATH=src JARVIS_HOME=/tmp/jarvis_dev python -m uvicorn jarvis.api.main:app --host 0.0.0.0 --port 8080 --reload` |
| Run tests | `PYTHONPATH=src JARVIS_HOME=/tmp/jarvis_test python -m pytest tests/ -v` |
| Lint | `ruff check src/ tests/` |

### Non-obvious caveats

- **PYTHONPATH must include `src/`** — the package lives under `src/jarvis/`.
- **JARVIS_HOME** — set to a writable temp dir (e.g. `/tmp/jarvis_dev`). The app creates `data/`, `logs/`, `skills/` etc. under this path.
- **DB writer** — started automatically by the FastAPI lifespan handler. All DB-touching endpoints require it.
- **LLM** — set `GROQ_API_KEY` (free at console.groq.com) for chat to work without a local Ollama instance.
- **edge-tts** — `test_tts_produces_audio` skips gracefully in cloud VMs with no egress to Microsoft's speech API.
- **Embedding** — `sentence-transformers` is excluded from `requirements-railway.txt`. Memory search falls back to FTS5 keyword-only when embeddings are unavailable.
