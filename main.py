"""JARVIS v7.0 — top-level FastAPI app.

This is the single entrypoint that wires every v7.0 subsystem together:

  * Agent loop          (core.agent)        — invokable via HTTP /agent/run
  * Red-Zone Sentinel   (core.sentinel)     — background asyncio task
  * KAIROS daemon       (core.kairos)       — background asyncio task
  * Voice WebSocket     (voice.websocket_server) — mounted at /voice
  * Observability       (observability.dashboard) — mounted at /obs
  * MCP router          (mcp.router)        — exposed via /mcp/* and
                                               registered as agent tools

Operational notes
-----------------
* The app uses FastAPI's lifespan to start/stop the daemons. If a
  daemon fails to start (e.g. asyncpg missing for KAIROS) the API
  continues to come up so the operator can inspect /healthz; the
  failure is logged and surfaced under /healthz.daemons.

* The MCP router is registered into core.agent so every tool exposed
  by the filesystem/network/automation servers is callable from the
  agent loop. Destructive tools are already pre-snapshotted by the
  agent (P3).

* All background tasks are auto-restarted with exponential backoff if
  they crash. Process-level supervision is still handled by systemd
  (see config/systemd/) — the in-process restart is the second layer.

Endpoints
---------
  GET  /healthz              — liveness + per-daemon status
  GET  /readyz               — readiness (db reachable + daemons ready)
  GET  /                     — operator landing page
  POST /agent/run            — run one agent turn  {"prompt": "..."}
  GET  /mcp/tools            — list MCP tools
  POST /mcp/call             — dispatch an MCP tool
  WS   /voice/voice          — voice pipeline WebSocket
  ANY  /obs/*                — observability dashboard
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Optional

# ─── Path setup so legacy 'src/' imports still work ───────────────────────

import sys
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "src"))
os.environ.setdefault("JARVIS_HOME", os.environ.get("JARVIS_HOME", "/tmp/jarvis"))


# ─── Logging ──────────────────────────────────────────────────────────────


def _logger() -> logging.Logger:
    try:
        from core import agent as _agent
        return _agent.get_logger()
    except Exception:
        log = logging.getLogger("jarvis.main")
        if not log.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            log.addHandler(h)
            log.setLevel(logging.INFO)
        return log


# ─── Supervised background tasks ─────────────────────────────────────────


class _Supervisor:
    """Run an awaitable coroutine factory with auto-restart on crash."""

    def __init__(self, name: str, factory, *,
                 backoff_min_s: float = 2.0,
                 backoff_max_s: float = 60.0):
        self.name = name
        self.factory = factory
        self.backoff_min_s = backoff_min_s
        self.backoff_max_s = backoff_max_s
        self.task: Optional[asyncio.Task] = None
        self.last_error: Optional[str] = None
        self.restart_count = 0
        self.started_at: Optional[float] = None
        self._stop = asyncio.Event()

    async def _runner(self) -> None:
        log = _logger()
        backoff = self.backoff_min_s
        self.started_at = time.time()
        while not self._stop.is_set():
            try:
                log.info("main.daemon.start", extra={"name": self.name})
                await self.factory()
                # Coroutine returned cleanly — usually means we asked it
                # to stop. Don't restart.
                log.info("main.daemon.exit_clean", extra={"name": self.name})
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = repr(e)
                self.restart_count += 1
                log.exception("main.daemon.crash", extra={
                    "name": self.name, "exc": repr(e),
                    "restart_count": self.restart_count,
                    "backoff_s": backoff,
                })
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(self.backoff_max_s, backoff * 2)

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._runner(), name=self.name)

    async def stop(self) -> None:
        self._stop.set()
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(BaseException):
                await self.task

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "running": bool(self.task and not self.task.done()),
            "restart_count": self.restart_count,
            "last_error": self.last_error,
            "started_at": self.started_at,
        }


# ─── MCP wiring ──────────────────────────────────────────────────────────


def _register_mcp_tools_into_agent() -> tuple[int, list[str]]:
    """Wire every MCP tool through the agent's tool registry.

    Returns (count_registered, errors).
    """
    log = _logger()
    errors: list[str] = []
    try:
        from core import agent as _agent
        from mcp import router as _mcp_router
    except Exception as e:
        errors.append(f"mcp/agent import failed: {e!r}")
        return 0, errors

    try:
        router = _mcp_router.get_router()
    except Exception as e:
        errors.append(f"mcp router load failed: {e!r}")
        return 0, errors

    n = 0
    for tool in router.list_tools():
        qualified = tool["qualified"]

        async def _handler(args: dict[str, Any], _q: str = qualified) -> Any:
            return await router.dispatch(_q, args)

        _agent.register_mcp_tool(qualified, _handler)
        n += 1
    log.info("main.mcp.registered", extra={"tools": n})
    return n, errors


# ─── Daemon factories ────────────────────────────────────────────────────


async def _run_sentinel() -> None:
    from core.sentinel import RedZoneSentinel
    sentinel = RedZoneSentinel()
    await sentinel.start()


async def _run_kairos() -> None:
    from core.kairos import Kairos
    kairos = Kairos()
    await kairos.start()


# ─── State exposed to handlers ───────────────────────────────────────────


class _AppState:
    supervisors: dict[str, _Supervisor] = {}
    mcp_tools_count: int = 0
    mcp_errors: list[str] = []
    started_at: float = 0.0


STATE = _AppState()


# ─── FastAPI app ─────────────────────────────────────────────────────────


def create_app() -> Any:
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError as e:
        raise RuntimeError(
            "fastapi not installed; pip install fastapi uvicorn") from e

    log = _logger()

    @asynccontextmanager
    async def lifespan(app):
        STATE.started_at = time.time()
        log.info("main.lifespan.start")

        # PaaS detection — Railway / Render / Fly all inject $PORT. When
        # we're on a managed platform we don't have iptables, auth.log,
        # or systemd, so default the Sentinel to dry-run instead of
        # crash-looping. The operator can still flip JARVIS_SENTINEL_DRY_RUN=false
        # explicitly to opt in.
        _on_paas = bool(
            os.environ.get("PORT")
            or os.environ.get("RAILWAY_ENVIRONMENT")
            or os.environ.get("RENDER")
            or os.environ.get("FLY_APP_NAME")
        )
        if _on_paas and "JARVIS_SENTINEL_DRY_RUN" not in os.environ:
            os.environ["JARVIS_SENTINEL_DRY_RUN"] = "true"

        # 1) Register MCP tools into the agent — wrapped because we
        # MUST NOT prevent the HTTP surface from coming up. Any MCP
        # failure ends up in STATE.mcp_errors and surfaces via /healthz.
        try:
            n, errs = _register_mcp_tools_into_agent()
            STATE.mcp_tools_count = n
            STATE.mcp_errors = errs
        except Exception as e:  # noqa: BLE001
            STATE.mcp_tools_count = 0
            STATE.mcp_errors = [f"register_mcp_tools_into_agent crashed: {e!r}"]
            log.exception("main.lifespan.mcp_init_failed",
                          extra={"exc": repr(e)})

        # 2) Start KAIROS (if enabled). On PaaS, KAIROS_ENABLED can be
        # turned off via env to save resources — otherwise it runs and
        # the supervisor restarts it on crash with exp. backoff.
        if os.environ.get("KAIROS_ENABLED", "true").lower() not in {"0", "false", "no", "off"}:
            sup = _Supervisor("kairos", _run_kairos)
            STATE.supervisors["kairos"] = sup
            sup.start()

        # 3) Start Sentinel (default on; dry-run lets it run in non-root envs)
        if os.environ.get("SENTINEL_ENABLED", "true").lower() not in {"0", "false", "no", "off"}:
            sup = _Supervisor("sentinel", _run_sentinel)
            STATE.supervisors["sentinel"] = sup
            sup.start()

        try:
            yield
        finally:
            log.info("main.lifespan.stop")
            await asyncio.gather(
                *(s.stop() for s in STATE.supervisors.values()),
                return_exceptions=True,
            )

    app = FastAPI(
        title="JARVIS v7.0",
        version="7.0.0",
        description="Just A Rather Very Intelligent System — integrated entrypoint.",
        lifespan=lifespan,
    )

    # ── Health / readiness ────────────────────────────────────────────────
    #
    # /health and /healthz are intentionally permissive — they return 200
    # as long as the FastAPI process is serving. PaaS health probes
    # (Railway, Render, Fly, Kubernetes) need a route that says "the
    # container is alive and listening" without depending on every
    # downstream (DB, daemons) being warm yet.
    #
    # For "is everything ready for traffic?" use /readyz, which actually
    # gates on daemon status and DB reachability.
    #
    # Both /health and /healthz exist because PaaS conventions disagree:
    # Railway/Render templates default to /health, Kubernetes idiomatic
    # is /healthz. Aliasing both costs us nothing and removes a footgun.

    async def _healthz_body() -> dict[str, Any]:
        return {
            "ok": True,
            "uptime_s": int(time.time() - STATE.started_at) if STATE.started_at else 0,
            "mcp_tools": STATE.mcp_tools_count,
            "mcp_errors": STATE.mcp_errors,
            "daemons": {n: s.status() for n, s in STATE.supervisors.items()},
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return await _healthz_body()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        # Alias for PaaS probes that default to /health (Railway, Render).
        return await _healthz_body()

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        problems: list[str] = []
        if STATE.mcp_tools_count == 0:
            problems.append("no MCP tools registered")
        for name, sup in STATE.supervisors.items():
            if not sup.status()["running"]:
                problems.append(f"daemon {name} not running")
        # Try a DB ping if asyncpg is available — but don't fail readiness
        # for systems running without Postgres (e.g. minimal profile).
        db_status = "skipped"
        try:
            from core import database as _db
            try:
                await _db.fetchval("SELECT 1")
                db_status = "ok"
            except Exception as e:
                db_status = f"error: {e!r}"
        except Exception:
            db_status = "asyncpg_missing"
        body = {"ok": not problems, "problems": problems, "db": db_status}
        return JSONResponse(body, status_code=200 if not problems else 503)

    # ── Root ──────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        rows = []
        for name, sup in STATE.supervisors.items():
            s = sup.status()
            color = "#a8e6a3" if s["running"] else "#ff8080"
            rows.append(
                f"<li><b style='color:{color}'>{name}</b> "
                f"restarts={s['restart_count']} "
                f"last_error={s['last_error'] or '-'}</li>"
            )
        html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>JARVIS v7.0</title>
<style>body{{font-family:ui-monospace,monospace;background:#0a0a0a;color:#e0e0e0;padding:24px}}
a{{color:#6cf}} h1{{color:#6cf}}</style></head><body>
<h1>JARVIS v7.0</h1>
<p>uptime: {int(time.time() - STATE.started_at) if STATE.started_at else 0}s ·
   mcp tools: {STATE.mcp_tools_count}</p>
<h2>daemons</h2><ul>{''.join(rows) or '<li>(none)</li>'}</ul>
<h2>endpoints</h2>
<ul>
  <li><a href="/healthz">/healthz</a> &middot; <a href="/readyz">/readyz</a></li>
  <li><a href="/mcp/tools">/mcp/tools</a></li>
  <li><a href="/obs/">/obs/</a> (observability dashboard)</li>
  <li><code>WS /voice/voice</code></li>
  <li><code>POST /agent/run</code> &nbsp; <code>{{"prompt": "..."}}</code></li>
</ul>
</body></html>"""
        return HTMLResponse(html)

    # ── Agent ─────────────────────────────────────────────────────────────

    @app.post("/agent/run")
    async def agent_run(payload: dict[str, Any]) -> dict[str, Any]:
        prompt = (payload or {}).get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(400, "missing 'prompt' (string)")
        try:
            from core.agent import run_jarvis_core
        except Exception as e:
            raise HTTPException(503, f"agent unavailable: {e!r}")
        # Build a minimal tool schema for the agent from MCP tools.
        try:
            from mcp.router import get_router
            mcp_tools = [
                {
                    "name": t["qualified"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema",
                                          {"type": "object",
                                           "properties": {},
                                           "additionalProperties": True}),
                }
                for t in get_router().list_tools()
            ]
        except Exception:
            mcp_tools = None
        run = await run_jarvis_core(
            prompt,
            tools=mcp_tools,
            system=payload.get("system"),
            max_iterations=int(payload.get("max_iterations", 10)),
        )
        return {
            "run_id": run.run_id,
            "iterations": run.iterations,
            "stopped_reason": run.stopped_reason,
            "final_message": run.final_message,
        }

    # ── MCP HTTP surface ──────────────────────────────────────────────────

    @app.get("/mcp/tools")
    async def mcp_tools() -> dict[str, Any]:
        try:
            from mcp.router import get_router
            return {"tools": get_router().list_tools()}
        except Exception as e:
            raise HTTPException(503, f"mcp unavailable: {e!r}")

    @app.post("/mcp/call")
    async def mcp_call(payload: dict[str, Any]) -> dict[str, Any]:
        tool = (payload or {}).get("tool")
        args = (payload or {}).get("args") or {}
        if not isinstance(tool, str) or not tool:
            raise HTTPException(400, "missing 'tool' (string)")
        if not isinstance(args, dict):
            raise HTTPException(400, "'args' must be an object")
        try:
            from mcp.router import dispatch
        except Exception as e:
            raise HTTPException(503, f"mcp unavailable: {e!r}")
        return await dispatch(tool, args)

    # ── Voice + observability sub-apps ────────────────────────────────────

    try:
        from voice.websocket_server import create_app as _voice_app
        app.mount("/voice", _voice_app())
        log.info("main.mount.voice", extra={"path": "/voice"})
    except Exception as e:
        log.warning("main.mount.voice_failed", extra={"exc": repr(e)})

    try:
        from observability.dashboard import create_app as _obs_app
        app.mount("/obs", _obs_app())
        log.info("main.mount.obs", extra={"path": "/obs"})
    except Exception as e:
        log.warning("main.mount.obs_failed", extra={"exc": repr(e)})

    return app


# ─── Module-level singleton (for `uvicorn main:app`) ──────────────────────


try:
    app = create_app()
except Exception as _e:  # pragma: no cover
    # Don't let import-time failures hide the cause; raise so the operator
    # sees it in the systemd log instead of a generic "no attribute 'app'".
    raise


# ─── CLI entrypoint ───────────────────────────────────────────────────────


def main() -> None:  # pragma: no cover
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("pip install fastapi uvicorn")
    host = os.environ.get("JARVIS_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("JARVIS_PORT", "8000")))
    uvicorn.run("main:app", host=host, port=port, log_level="info",
                reload=os.environ.get("JARVIS_DEV_RELOAD") == "1")


if __name__ == "__main__":  # pragma: no cover
    main()
