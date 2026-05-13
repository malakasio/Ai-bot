import sys
import os
from anthropic import Anthropic

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
os.environ.setdefault("JARVIS_HOME", "/tmp/jarvis")

from jarvis.api.main import app

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

Anthropic.api_key = os.environ["ANTHROPIC_API_KEY"]
Anthropic.base_url = "https://anthropic-api.com/v1"