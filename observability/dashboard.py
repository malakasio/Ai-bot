"""Real-time observability dashboard.

A FastAPI app that:

  * ``GET /healthz``    — liveness probe.
  * ``GET /metrics``    — recent rows from the ``metrics`` table.
  * ``GET /trace/tail``       — last N lines of the trace file (JSONL).
  * ``GET /trace/stream``     — Server-Sent Events stream of new trace
                                 lines as they're written.
  * ``WS  /trace/ws``         — WebSocket variant of the same stream.
  * ``GET /``                  — minimal HTML page that auto-connects to
                                 /trace/stream and renders rows live.

Importing this module is safe without FastAPI; only ``create_app()``
requires it.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from . import tracing


# ─── HTML for the / page ──────────────────────────────────────────────────


_HTML_INDEX = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>JARVIS observability</title>
<style>
  body { font-family: ui-monospace, Menlo, monospace; background: #0a0a0a;
         color: #e0e0e0; margin: 0; padding: 16px; }
  h1 { font-size: 1.1rem; margin: 0 0 12px; color: #6cf; }
  .row { white-space: pre-wrap; padding: 4px 8px; border-bottom: 1px solid #1c1c1c; }
  .row.error { color: #ff8080; }
  .row.warn  { color: #f5c842; }
  .row.info  { color: #a8e6a3; }
  .meta { color: #888; }
  #count { color: #888; font-size: 0.9rem; }
</style>
</head>
<body>
<h1>JARVIS observability — <span id="count">0</span> events</h1>
<div id="log"></div>
<script>
  const log = document.getElementById("log");
  const counter = document.getElementById("count");
  let n = 0;
  const es = new EventSource("/trace/stream");
  es.onmessage = (ev) => {
    let obj;
    try { obj = JSON.parse(ev.data); }
    catch { obj = { raw: ev.data }; }
    const div = document.createElement("div");
    const lvl = (obj.level || "INFO").toLowerCase();
    div.className = "row " + lvl;
    const ts = obj.ts || "";
    const event = obj.event || "";
    const rest = Object.fromEntries(Object.entries(obj).filter(
      ([k]) => !["ts","level","event","logger"].includes(k)));
    div.innerHTML = `<span class="meta">${ts}</span> [${(obj.level||"").padEnd(5)}] ${event} `
                  + `<span class="meta">${JSON.stringify(rest)}</span>`;
    log.prepend(div);
    n += 1;
    counter.textContent = n;
    while (log.childNodes.length > 500) log.removeChild(log.lastChild);
  };
  es.onerror = () => { /* allow auto-reconnect */ };
</script>
</body>
</html>
"""


# ─── File tailing ─────────────────────────────────────────────────────────


async def _tail_lines(
    path: Path, *, poll_s: float = 0.5, from_end: bool = True
) -> AsyncIterator[str]:
    """Yield new lines appended to ``path``. Re-opens after rotation."""
    # Wait for the file to exist.
    while not path.exists():
        await asyncio.sleep(poll_s)
    f = open(path, "r", encoding="utf-8", errors="replace")
    inode = os.fstat(f.fileno()).st_ino
    if from_end:
        f.seek(0, 2)
    try:
        while True:
            line = f.readline()
            if line:
                yield line.rstrip("\n")
                continue
            # No data; check for rotation and sleep.
            await asyncio.sleep(poll_s)
            try:
                cur_inode = os.stat(path).st_ino
            except FileNotFoundError:
                continue
            if cur_inode != inode:
                # File rotated under us; reopen from the start.
                try:
                    f.close()
                except Exception:
                    pass
                while not path.exists():
                    await asyncio.sleep(poll_s)
                f = open(path, "r", encoding="utf-8", errors="replace")
                inode = os.fstat(f.fileno()).st_ino
    finally:
        try:
            f.close()
        except Exception:
            pass


def _read_tail(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in f.readlines()[-n:]]
    except OSError:
        return []


# ─── FastAPI app factory ──────────────────────────────────────────────────


def create_app() -> Any:
    try:
        from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
        from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    except ImportError as e:
        raise RuntimeError("fastapi not installed; pip install fastapi uvicorn") from e

    app = FastAPI(title="JARVIS observability dashboard", version="7.0")
    started_at = time.time()

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "uptime_s": int(time.time() - started_at),
                "trace_path": str(tracing.trace_path()),
            }
        )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_HTML_INDEX)

    @app.get("/trace/tail")
    async def trace_tail(n: int = Query(default=200, ge=1, le=10000)) -> JSONResponse:
        lines = _read_tail(tracing.trace_path(), n)
        out: list[dict[str, Any]] = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except Exception:
                out.append({"raw": line})
        return JSONResponse({"path": str(tracing.trace_path()), "events": out})

    @app.get("/trace/stream")
    async def trace_stream(request: Request) -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            # Send any tail buffer first so the page isn't empty on open.
            for line in _read_tail(tracing.trace_path(), 50):
                if await request.is_disconnected():
                    return
                yield f"data: {line}\n\n".encode("utf-8")
            async for line in _tail_lines(tracing.trace_path()):
                if await request.is_disconnected():
                    return
                yield f"data: {line}\n\n".encode("utf-8")

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.websocket("/trace/ws")
    async def trace_ws(ws: WebSocket) -> None:
        await ws.accept()
        try:
            for line in _read_tail(tracing.trace_path(), 50):
                await ws.send_text(line)
            async for line in _tail_lines(tracing.trace_path()):
                await ws.send_text(line)
        except WebSocketDisconnect:
            return
        except Exception:
            return

    @app.get("/metrics")
    async def metrics_recent(
        limit: int = Query(default=50, ge=1, le=1000),
        actor: Optional[str] = Query(default=None),
        tool: Optional[str] = Query(default=None),
    ) -> JSONResponse:
        try:
            from core import database

            await tracing.ensure_metrics_schema()
            clauses = []
            args: list[Any] = []
            if actor:
                args.append(actor)
                clauses.append(f"actor = ${len(args)}")
            if tool:
                args.append(tool)
                clauses.append(f"tool = ${len(args)}")
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            args.append(limit)
            rows = await database.fetch(
                f"""
                SELECT id, ts, task_id, actor, tool, score, duration_s,
                       input_tokens, output_tokens, exit_code, exceptions,
                       tests_total, tests_passed, breakdown, notes
                FROM metrics
                {where}
                ORDER BY ts DESC
                LIMIT ${len(args)}
                """,
                *args,
            )
            return JSONResponse({"rows": [dict(r) for r in rows]})
        except Exception as e:
            return JSONResponse({"rows": [], "error": repr(e)}, status_code=503)

    @app.get("/metrics/summary")
    async def metrics_summary() -> JSONResponse:
        try:
            from core import database

            await tracing.ensure_metrics_schema()
            stats = await database.fetchrow(
                """
                SELECT
                  count(*)::int AS total,
                  avg(score)::float AS avg_score,
                  min(score)::int AS min_score,
                  max(score)::int AS max_score,
                  sum(CASE WHEN exit_code IS NOT NULL AND exit_code <> 0
                       THEN 1 ELSE 0 END)::int AS error_count
                FROM metrics
                WHERE ts > now() - interval '24 hours'
                """
            )
            return JSONResponse(dict(stats or {}))
        except Exception as e:
            return JSONResponse({"error": repr(e)}, status_code=503)

    return app


# ─── CLI ──────────────────────────────────────────────────────────────────


def main() -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(prog="observability.dashboard")
    parser.add_argument("--host", default=os.environ.get("OBS_DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("OBS_DASHBOARD_PORT", "8090"))
    )
    args = parser.parse_args()
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("pip install fastapi uvicorn")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
