"""JARVIS v7.0 — top-level FastAPI app.

This is the single entrypoint that wires every v7.0 subsystem together:

  * Agent loop          (core.agent)        — invokable via HTTP /agent/run
  * Red-Zone Sentinel   (core.sentinel)     — background asyncio task
  * KAIROS daemon       (core.kairos)       — background asyncio task
  * Voice WebSocket     (voice.websocket_server) — mounted at /voice
  * Observability       (observability.dashboard) — mounted at /obs
  * MCP router          (mcp.router)        — exposed via /mcp/* and
                                               registered as agent tools

Deployment-hardening notes
--------------------------
The PaaS health check is the bottleneck of every deploy. Railway,
Render and Fly all decide "did the service become ready?" by polling
GET /healthz (or /health). If that probe doesn't get a 200 inside
the platform's window — Railway's default is two minutes — the
container is killed and the deploy is marked failed.

This module is structured to make that probe succeed within
milliseconds, no matter what else is broken:

  1. /healthz and /health are bound on the FastAPI app BEFORE the
     lifespan runs, before any subsystem import. The probe handler
     does not import core.agent, mcp.router, or any other v7.0
     module — it just returns {"ok": true}.
  2. Sub-app mounts (voice, observability) happen INSIDE lifespan,
     each wrapped in try/except. A broken voice mount cannot prevent
     the rest of the app from serving.
  3. MCP-tool registration into the agent is opportunistic. It logs
     errors into /healthz.mcp_errors but never raises.
  4. Daemon supervisors only start AFTER the app has yielded
     (handled by asyncio.create_task in _Supervisor.start), so the
     port is bound and accepting traffic before any slow init runs.
  5. core.agent is imported lazily — never at module load time — so
     a missing anthropic SDK on the build platform cannot break
     `uvicorn main:app`.

Endpoints
---------
  GET  /healthz              — liveness (zero-dependency, always 200)
  GET  /health               — alias for PaaS conventions
  GET  /readyz               — readiness (db reachable + daemons ready)
  GET  /metrics              — Prometheus metrics (LLM tokens, costs, latency)
  GET  /                     — operator landing page
  POST /agent/run            — run one agent turn  {"prompt": "..."}
  GET  /mcp/tools            — list MCP tools
  POST /mcp/call             — dispatch an MCP tool
  WS   /voice/voice          — voice pipeline WebSocket (mounted in lifespan)
  ANY  /obs/*                — observability dashboard (mounted in lifespan)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Optional

# ─── Path setup so legacy 'src/' imports still work ───────────────────────

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "src"))
os.environ.setdefault("JARVIS_HOME", os.environ.get("JARVIS_HOME", "/tmp/jarvis"))


# ─── Lightweight fallback logger ─────────────────────────────────────────
#
# We intentionally do NOT import core.agent at module load. The trace
# logger there does filesystem I/O (creates /var/log/jarvis_agent.trace),
# which can fail in restrictive PaaS containers and would take the
# import down with it. We fall back to stdlib logging until lifespan
# decides we have time for the richer logger.


def _logger() -> logging.Logger:
    log = logging.getLogger("jarvis.main")
    if not log.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
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
                log.info("daemon.%s.start", self.name)
                await self.factory()
                log.info("daemon.%s.exit_clean", self.name)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = repr(e)
                self.restart_count += 1
                log.exception("daemon.%s.crash (restart=%d, backoff=%.1fs)",
                              self.name, self.restart_count, backoff)
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


# ─── State exposed to handlers ───────────────────────────────────────────


class _AppState:
    supervisors: dict[str, _Supervisor] = {}
    mcp_tools_count: int = 0
    mcp_errors: list[str] = []
    mount_errors: list[str] = []
    started_at: float = 0.0
    lifespan_started: bool = False
    lifespan_complete: bool = False


STATE = _AppState()


# ─── MCP wiring (opportunistic — never raises) ───────────────────────────


def _register_mcp_tools_into_agent() -> tuple[int, list[str]]:
    """Wire every MCP tool through the agent's tool registry.

    Returns (count_registered, errors). Never raises — any failure ends
    up in the errors list and surfaces via /healthz.mcp_errors.
    """
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
    try:
        for tool in router.list_tools():
            qualified = tool["qualified"]

            async def _handler(args: dict[str, Any], _q: str = qualified) -> Any:
                return await router.dispatch(_q, args)

            _agent.register_mcp_tool(qualified, _handler)
            n += 1
    except Exception as e:
        errors.append(f"mcp tool registration failed: {e!r}")
    return n, errors


# ─── Daemon factories (imports deferred to call time) ───────────────────


async def _run_sentinel() -> None:
    from core.sentinel import RedZoneSentinel
    sentinel = RedZoneSentinel()
    await sentinel.start()


async def _run_kairos() -> None:
    from core.kairos import Kairos
    kairos = Kairos()
    await kairos.start()


async def _run_telegram() -> None:
    """Inbound Telegram bot daemon. Returns clean when disabled or
    when python-telegram-bot is missing — the supervisor treats that
    as a normal shutdown and won't restart-loop.
    """
    from core.telegram_bot import start_telegram_bot
    await start_telegram_bot()


# ─── FastAPI app ─────────────────────────────────────────────────────────


def create_app() -> Any:
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
    except ImportError as e:
        raise RuntimeError(
            "fastapi not installed; pip install fastapi uvicorn") from e

    log = _logger()

    @asynccontextmanager
    async def lifespan(app):
        STATE.started_at = time.time()
        STATE.lifespan_started = True
        log.info("lifespan.start")

        # 0) Init PostgreSQL connection pool (asyncpg).
        try:
            from core import database as _db
            await _db.init()
            log.info("lifespan.db_pool_ready")
        except Exception as e:
            STATE.mount_errors.append(f"db_pool: {e!r}")
            log.warning("lifespan.db_pool_failed: %r", e)

        # PaaS detection — Railway / Render / Fly all inject $PORT. When
        # we're on a managed platform we don't have iptables, auth.log,
        # or systemd, so default the Sentinel to dry-run instead of
        # crash-looping. Operators can flip JARVIS_SENTINEL_DRY_RUN=false
        # explicitly to opt in.
        _on_paas = bool(
            os.environ.get("PORT")
            or os.environ.get("RAILWAY_ENVIRONMENT")
            or os.environ.get("RENDER")
            or os.environ.get("FLY_APP_NAME")
        )
        if _on_paas:
            if "JARVIS_SENTINEL_DRY_RUN" not in os.environ:
                os.environ["JARVIS_SENTINEL_DRY_RUN"] = "true"
            if "KAIROS_DRY_RUN" not in os.environ:
                os.environ["KAIROS_DRY_RUN"] = "true"

        # 1) MCP tool registration. Wrapped — never crashes lifespan.
        try:
            n, errs = _register_mcp_tools_into_agent()
            STATE.mcp_tools_count = n
            STATE.mcp_errors = errs
        except Exception as e:  # noqa: BLE001
            STATE.mcp_tools_count = 0
            STATE.mcp_errors = [f"register_mcp_tools_into_agent crashed: {e!r}"]
            log.exception("lifespan.mcp_init_failed")

        # 2) Mount sub-apps. Done in lifespan rather than create_app so
        # an import error in voice/obs cannot prevent the root app from
        # serving /healthz.
        if os.environ.get("VOICE_ENABLED", "true").lower() not in {"0", "false", "no", "off"}:
            try:
                from voice.websocket_server import create_app as _voice_app
                app.mount("/voice", _voice_app())
                log.info("mount.voice ok")
            except Exception as e:  # noqa: BLE001
                STATE.mount_errors.append(f"voice: {e!r}")
                log.warning("mount.voice failed: %r", e)

        if os.environ.get("OBS_ENABLED", "true").lower() not in {"0", "false", "no", "off"}:
            try:
                from observability.dashboard import create_app as _obs_app
                app.mount("/obs", _obs_app())
                log.info("mount.obs ok")
            except Exception as e:  # noqa: BLE001
                STATE.mount_errors.append(f"obs: {e!r}")
                log.warning("mount.obs failed: %r", e)

        # 3) Start daemons (each in its own supervised task; the lifespan
        # itself does NOT await them — it yields immediately).
        if os.environ.get("KAIROS_ENABLED", "true").lower() not in {"0", "false", "no", "off"}:
            sup = _Supervisor("kairos", _run_kairos)
            STATE.supervisors["kairos"] = sup
            sup.start()

        if os.environ.get("SENTINEL_ENABLED", "true").lower() not in {"0", "false", "no", "off"}:
            sup = _Supervisor("sentinel", _run_sentinel)
            STATE.supervisors["sentinel"] = sup
            sup.start()

        # Inbound Telegram bot. Started only if (a) the env opt-in is on
        # AND (b) the bot is actually configured — token + allowed user id.
        # The supervisor exits cleanly when the bot is not configured, so
        # the absence of credentials is not an error.
        if os.environ.get("TELEGRAM_ENABLED", "true").lower() not in {"0", "false", "no", "off"}:
            try:
                from core.telegram_bot import is_enabled as _tg_enabled
                _tg_configured = _tg_enabled()
            except Exception as e:  # noqa: BLE001
                STATE.mount_errors.append(f"telegram_bot import: {e!r}")
                _tg_configured = False
            if _tg_configured:
                sup = _Supervisor("telegram", _run_telegram)
                STATE.supervisors["telegram"] = sup
                sup.start()
            else:
                log.info("telegram bot not configured "
                         "(set TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID to enable)")

        STATE.lifespan_complete = True
        log.info("lifespan.ready: port bound, daemons supervised")

        try:
            yield
        finally:
            log.info("lifespan.stop")
            # Close DB pool before shutting down daemons.
            try:
                from core import database as _db
                await _db.close()
                log.info("lifespan.db_pool_closed")
            except Exception as e:
                log.warning("lifespan.db_pool_close_failed: %r", e)
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

    # ── Health (registered FIRST so even a totally broken lifespan
    # cannot prevent /healthz from returning 200) ─────────────────────────
    #
    # These handlers MUST NOT import anything heavy and MUST NOT touch
    # STATE in a way that could raise. If you can't compute it without
    # branching, default it. The PaaS probe is the most important
    # consumer; correctness matters less than availability.

    def _healthz_body() -> dict[str, Any]:
        try:
            uptime = int(time.time() - STATE.started_at) if STATE.started_at else 0
        except Exception:
            uptime = 0
        try:
            daemons = {n: s.status() for n, s in STATE.supervisors.items()}
        except Exception:
            daemons = {}
        return {
            "ok": True,
            "uptime_s": uptime,
            "lifespan_started": STATE.lifespan_started,
            "lifespan_complete": STATE.lifespan_complete,
            "mcp_tools": STATE.mcp_tools_count,
            "mcp_errors": STATE.mcp_errors,
            "mount_errors": STATE.mount_errors,
            "daemons": daemons,
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return _healthz_body()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        # Alias for PaaS probes that default to /health (Railway, Render).
        return _healthz_body()

    @app.get("/ping", response_class=PlainTextResponse)
    async def ping() -> str:
        # The lightest probe possible — pure string, no JSON, no STATE access.
        # Useful as a fallback healthcheck path.
        return "pong"

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        """Prometheus metrics endpoint."""
        try:
            from core.metrics import get_metrics
            return get_metrics().export_prometheus()
        except Exception as e:
            return f"# Error exporting metrics: {e!r}\n"

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        problems: list[str] = []
        if not STATE.lifespan_complete:
            problems.append("lifespan still warming up")
        for name, sup in STATE.supervisors.items():
            if not sup.status()["running"]:
                problems.append(f"daemon {name} not running")
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

    # ── Root landing ──────────────────────────────────────────────────────

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
   mcp tools: {STATE.mcp_tools_count} ·
   lifespan: {'ready' if STATE.lifespan_complete else 'warming'}</p>
<h2>daemons</h2><ul>{''.join(rows) or '<li>(none)</li>'}</ul>
<h2>endpoints</h2>
<ul>
  <li><a href="/healthz">/healthz</a> &middot; <a href="/health">/health</a> &middot; <a href="/ping">/ping</a> &middot; <a href="/readyz">/readyz</a></li>
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

        # ── Load last 10 episodes as conversation context ────────────────
        context = ""
        try:
            from core.memory import recent_episodes, semantic_episodes
            episodes = await recent_episodes(limit=10)

            # Feature flag: add semantic retrieval
            if os.getenv("JARVIS_ENABLE_SEMANTIC_MEMORY", "false").lower() == "true":
                try:
                    # Extract query from user prompt
                    if prompt:
                        semantic_eps = await semantic_episodes(
                            query=prompt,
                            limit=5,
                            min_confidence=0.7
                        )
                        # Merge: temporal (10) + semantic (up to 5)
                        episodes = episodes + semantic_eps
                except Exception as e:
                    # Semantic search failed, continue with temporal only
                    import sys
                    print(f"[main] Semantic retrieval failed: {e}", file=sys.stderr)

            if episodes:
                lines = [
                    f"[{ep['ts']}] {ep['actor']}/{ep.get('tool', ep.get('kind', 'unknown'))}: "
                    f"{ep.get('input', ep.get('subject', ''))} -> {ep.get('output', ep.get('content', ''))}"
                    for ep in episodes
                ]
                context = (
                    "Recent conversation history (last 10 episodes):\n"
                    + "\n".join(lines)
                )
        except Exception:
            pass  # memory store unavailable — proceed without context

        system_prompt = payload.get("system")
        if context:
            system_prompt = (
                f"{context}\n\n{system_prompt or ''}"
            ).strip()

        run = await run_jarvis_core(
            prompt,
            tools=mcp_tools,
            system=system_prompt or None,
            max_iterations=int(payload.get("max_iterations", 10)),
        )

        # ── Store this episode ──────────────────────────────────────────
        try:
            from core.memory import Episode as _Ep, store_episode
            await store_episode(_Ep(
                actor="user",
                tool="agent/run",
                input=prompt[:1024],
                output=(run.final_message or "")[:2048],
                metadata={
                    "run_id": run.run_id,
                    "iterations": run.iterations,
                    "stopped_reason": run.stopped_reason,
                },
            ))
        except Exception:
            pass  # memory store unavailable — non-fatal

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

    return app


# ─── Module-level singleton (for `uvicorn main:app`) ──────────────────────


app = create_app()


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
