## Cursor Cloud specific instructions

### Project overview

JARVIS v6.0 is a Python 3.11 FastAPI autonomous assistant. See `README.md` for full architecture.

### Quick reference

| Action | Command |
|--------|---------|
| Activate venv | `source /workspace/.venv/bin/activate` |
| Run API (dev) | `PYTHONPATH=src JARVIS_HOME=/tmp/jarvis_dev python -m uvicorn jarvis.api.main:app --host 0.0.0.0 --port 8080 --reload` |
| Run tests | `PYTHONPATH=src JARVIS_HOME=/tmp/jarvis_test python -m pytest tests/ -v` |
| Lint | `ruff check src/ tests/` |
| Start Ollama | `ollama serve` (in background) |

### Non-obvious caveats

- **PYTHONPATH must include `src/`**: The package lives under `src/jarvis/`, so always set `PYTHONPATH=src` (or `/workspace/src`) when running the app or tests.
- **JARVIS_HOME**: Set this env var to a writable temp directory (e.g., `/tmp/jarvis_dev`) to avoid creating directories in system paths. The app creates `data/`, `logs/`, `skills/`, etc. under this path.
- **Ollama is required for the free stack**: The `/chat` endpoint and agent system route to Ollama by default. Start `ollama serve` before using chat features. The model `llama3.2:3b` must be pulled (`ollama pull llama3.2:3b`).
- **DB writer lifespan**: The FastAPI app starts the SQLite DB writer via a lifespan handler. Any endpoint that touches the database (chat, tasks, memory) requires this to be running. This was a bug fix added during setup.
- **edge-tts test fails in cloud VMs**: `test_tts_produces_audio` fails with a 403 from Microsoft's speech API when running in restricted network environments. This is expected and not a code issue.
- **`.env` file**: Copy `.env.example` to `.env` for local config. No API keys are required for the free stack.
