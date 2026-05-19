import sys
import os
from anthropic import Anthropic

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
os.environ.setdefault("JARVIS_HOME", "/tmp/jarvis")

from jarvis.api.main import app

# Διασφαλίζει ότι το κλειδί φορτώνεται πριν τη χρήση
if "ANTHROPIC_API_KEY" not in os.environ:
    raise EnvironmentError("ANTHROPIC_API_KEY is not set in environment variables")

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)