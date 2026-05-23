"""JARVIS v7.0 integration smoke tests.

These tests verify the wiring done in main.py:

  * FastAPI app boots (lifespan starts daemons, registers MCP tools).
  * /healthz reports daemon state.
  * /mcp/tools lists tools from the MCP router.
  * Sentinel and KAIROS can be imported and instantiated.
  * The agent module's symbol surface matches what main.py uses.

They deliberately avoid:
  * Real Anthropic API calls (no key needed in CI).
  * Real Postgres (DB ping is allowed to fail; readyz reports it).
  * Real systemd / iptables / Telegram side-effects.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make sure the repo root is on sys.path even if the test runner doesn't add it.
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

os.environ.setdefault("JARVIS_HOME", "/tmp/jarvis_test_v7")
os.environ.setdefault("JARVIS_SENTINEL_DRY_RUN", "true")
os.environ.setdefault("KAIROS_DRY_RUN", "true")
# Keep daemons quiet during tests.
os.environ.setdefault("KAIROS_INTERVAL", "3600")
os.environ.setdefault("JARVIS_SENTINEL_INTEGRITY_INTERVAL_S", "3600")


# ─── Imports ──────────────────────────────────────────────────────────────


class TestImports:
    def test_core_agent_imports(self):
        from core import agent

        assert hasattr(agent, "run_jarvis_core")
        assert hasattr(agent, "register_mcp_tool")
        assert hasattr(agent, "DESTRUCTIVE_TOOLS")

    def test_core_sentinel_imports(self):
        from core import sentinel

        assert hasattr(sentinel, "RedZoneSentinel")
        assert hasattr(sentinel, "SentinelConfig")

    def test_core_kairos_imports(self):
        from core import kairos

        assert hasattr(kairos, "Kairos")
        assert hasattr(kairos, "auto_dream")

    def test_mcp_router_imports(self):
        from mcp import router

        assert hasattr(router, "get_router")
        assert hasattr(router, "dispatch")

    def test_voice_pipeline_imports(self):
        # Voice modules are import-safe even when API keys / websockets
        # aren't configured; PipelineError only surfaces on instantiation.
        from voice import pipeline, websocket_server

        assert hasattr(pipeline, "VoicePipeline")
        assert hasattr(websocket_server, "create_app")

    def test_observability_imports(self):
        from observability import dashboard, tracing

        assert hasattr(dashboard, "create_app")
        assert hasattr(tracing, "trace_path") or hasattr(tracing, "TRACE_LOG_PATH")


# ─── MCP router ───────────────────────────────────────────────────────────


class TestMCPRouter:
    def test_router_loads(self):
        from mcp.router import get_router

        router = get_router()
        servers = list(router.servers.keys())
        # config/mcp_config.json enables filesystem, network, automation.
        assert "filesystem" in servers
        assert "network" in servers
        assert "automation" in servers

    def test_tools_listable(self):
        from mcp.router import get_router

        tools = get_router().list_tools()
        assert len(tools) > 0
        names = {t["qualified"] for t in tools}
        assert "filesystem.read_file" in names


# ─── main.py wiring ───────────────────────────────────────────────────────


class TestMainApp:
    def test_create_app_succeeds(self):
        # Importing main builds the FastAPI app at module load.
        import main

        assert main.app is not None
        # Routes we promised in the docstring.
        routes = {getattr(r, "path", None) for r in main.app.routes}
        assert "/healthz" in routes
        # PaaS probes (Railway/Render default) hit /health, not /healthz.
        # Regression: /health must be aliased.
        assert "/health" in routes
        assert "/readyz" in routes
        assert "/agent/run" in routes
        assert "/mcp/tools" in routes
        assert "/mcp/call" in routes

    def test_mcp_tools_registered_with_agent(self):
        # Importing main runs MCP -> agent registration.
        import main  # noqa: F401
        from core.agent import _MCP_TOOLS

        # After app boot _register_mcp_tools_into_agent is called from
        # lifespan, but we also call it directly here so the test doesn't
        # require running the lifespan.
        n, errs = main._register_mcp_tools_into_agent()
        assert n > 0, f"no MCP tools registered; errors={errs}"
        assert any(name.startswith("filesystem.") for name in _MCP_TOOLS)


# ─── Sentinel / KAIROS smoke ─────────────────────────────────────────────


class TestSentinelSmoke:
    def test_config_dry_run(self):
        from core.sentinel import SentinelConfig

        cfg = SentinelConfig()
        # Env we set above honored
        assert cfg.dry_run is True

    @pytest.mark.asyncio
    async def test_trigger_lockdown_dry_run(self):
        from core.sentinel import RedZoneSentinel, SentinelConfig

        cfg = SentinelConfig()
        s = RedZoneSentinel(cfg)
        report = await s.trigger_lockdown(
            ip="192.0.2.1",
            reason="unit-test",
            evidence="synthetic",
        )
        assert report["dry_run"] is True
        # block_ip / snapshot / restart_services should all skip in dry-run.
        for step, val in report["steps"].items():
            if isinstance(val, dict):
                # Either skipped (dry_run / not_root) or telegram step.
                assert ("skipped" in val) or step == "telegram"


class TestKairosSmoke:
    @pytest.mark.asyncio
    async def test_dry_run_tick(self):
        from core.kairos import Kairos, KairosConfig

        cfg = KairosConfig()
        cfg.dry_run = True
        cfg.task_batch = 1
        k = Kairos(cfg)
        out = await k.tick()
        assert out["tick"] == 1
        # health key always present
        assert "health" in out


# ─── Logger collision regression ─────────────────────────────────────────


class TestLoggerExtraCollision:
    """Regression: logger.info(..., extra={"name": "x"}) raised
    KeyError("Attempt to overwrite 'name' in LogRecord") and crashed the
    KAIROS / Sentinel daemons on startup. The fix sanitizes `extra` via a
    LoggerAdapter that renames colliding keys to `extra_<key>`.
    """

    def test_reserved_keys_do_not_crash(self):
        from core.agent import get_logger

        log = get_logger()
        # Every reserved LogRecord attribute name passed via extra.
        # Without the fix the first call raises KeyError.
        bad = {
            "name": "kairos",
            "message": "x",
            "module": "y",
            "args": [1, 2],
            "msg": "z",
            "level": "INFO",
            "thread": 42,
        }
        log.info("kairos.start", extra=bad)  # must not raise
        log.warning("ping", extra={"name": "sentinel", "ip": "192.0.2.1"})

    def test_kairos_start_log_does_not_raise(self):
        # Re-run the exact call site that crashed the daemon: log the
        # KairosConfig as extra, which has a `name`-ish 'name' field via
        # dataclasses.asdict will not have but other reserved keys can sneak
        # in via metadata.
        import dataclasses
        from core.kairos import KairosConfig
        from core.agent import get_logger

        cfg = KairosConfig()
        log = get_logger()
        log.info("kairos.start", extra=dataclasses.asdict(cfg))


# ─── Healthz via TestClient ──────────────────────────────────────────────


class TestHealthz:
    def test_healthz_returns_ok(self):
        # Use FastAPI's TestClient to exercise the actual handler.
        try:
            from fastapi.testclient import TestClient
        except Exception:
            pytest.skip("fastapi[testclient] not installed")
        import main

        with TestClient(main.app) as client:
            resp = client.get("/healthz")
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is True
            assert "daemons" in body
            assert "mcp_tools" in body

    def test_health_alias_returns_ok(self):
        """Regression: PaaS health probes hit /health (Railway/Render default).

        The original deploy failed because /health 404'd while /healthz
        worked. Both must return the same body now.
        """
        try:
            from fastapi.testclient import TestClient
        except Exception:
            pytest.skip("fastapi[testclient] not installed")
        import main

        with TestClient(main.app) as client:
            r1 = client.get("/health")
            r2 = client.get("/healthz")
            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r1.json().keys() == r2.json().keys()
            assert r1.json()["ok"] is True

    def test_mcp_tools_endpoint(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            pytest.skip("fastapi[testclient] not installed")
        import main

        with TestClient(main.app) as client:
            resp = client.get("/mcp/tools")
            assert resp.status_code == 200
            body = resp.json()
            assert "tools" in body
            assert len(body["tools"]) > 0
