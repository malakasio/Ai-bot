"""Root-level entry point for Railway/Railpack auto-detection."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
os.environ.setdefault("JARVIS_HOME", "/tmp/jarvis")

from jarvis.api.main import app  # noqa: F401 — imported for uvicorn

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
