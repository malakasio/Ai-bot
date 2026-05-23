"""Re-export FastAPI app and middleware from main.py."""

import sys
from pathlib import Path
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Add parent directory to path to import main
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from main import app


class LimitBodySize(BaseHTTPMiddleware):
    """Middleware to limit request body size."""

    def __init__(self, app, max_size: int = 10 * 1024 * 1024):
        super().__init__(app)
        self.max_size = max_size

    async def dispatch(self, request: Request, call_next):
        """Check content-length header before processing request."""
        content_length = request.headers.get("content-length")

        if content_length:
            try:
                size = int(content_length)
                if size > self.max_size:
                    return JSONResponse(
                        status_code=413,
                        content={"error": "Request body too large"}
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"error": "Invalid Content-Length header"}
                )

        return await call_next(request)


# Add test endpoint for middleware testing
@app.post("/chat")
async def chat_endpoint(request: Request):
    """Test endpoint for middleware validation."""
    return JSONResponse({"status": "ok"})


# Add the middleware to the app
app.add_middleware(LimitBodySize)


__all__ = ['app', 'LimitBodySize']
