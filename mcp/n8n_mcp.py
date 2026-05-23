"""MCP server: n8n — stdio bridge to the real czlonkowski/n8n-mcp Node.js process.

Replaces the pre-network local stand-in (commit d3f2787) with a genuine
MCP JSON-RPC bridge. Spawns ``n8n-mcp`` as a subprocess, proxies every
tool call through the real upstream binary.

Env:
  N8N_BASE_URL  — n8n instance URL (default: http://localhost:5678)
  N8N_API_KEY   — n8n API key from Settings → n8n API
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import time
from typing import Any, Optional

from ._common import Server, Tool, err, ok, require_dict

log = logging.getLogger("jarvis.mcp.n8n")

# ─── Configuration ───────────────────────────────────────────────────────

N8N_MCP_BIN = shutil.which("n8n-mcp") or "n8n-mcp"
STARTUP_TIMEOUT_S = int(os.environ.get("MCP_STARTUP_TIMEOUT", "30"))
REQUEST_TIMEOUT_S = int(os.environ.get("MCP_REQUEST_TIMEOUT", "120"))
MAX_RESTARTS = int(os.environ.get("MCP_MAX_RESTARTS", "2"))

server = Server(name="n8n")


# ─── Known tool stubs (from real n8n-mcp v2.55) ──────────────────────────
#
# Hardcoded so get_server() returns instantly; the subprocess starts lazily
# on the first tool call inside the app's event loop.

_N8N_MCP_TOOLS: list[dict[str, Any]] = [
    {"name": "n8n_list_workflows", "desc": "List workflows, optionally filtered by project/status/tags.", "schema": {"type": "object", "properties": {"projectId": {"type": "string"}, "active": {"type": "boolean"}, "tags": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer"}}}},
    {"name": "n8n_get_workflow", "desc": "Get workflow by ID. Modes: full, details, structure, minimal, active.", "schema": {"type": "object", "properties": {"id": {"type": "string"}, "mode": {"type": "string", "enum": ["full", "details", "structure", "minimal", "active"]}}, "required": ["id"]}},
    {"name": "n8n_create_workflow", "desc": "Create workflow. Requires name, nodes[], connections{}. Created inactive.", "schema": {"type": "object", "properties": {"name": {"type": "string"}, "nodes": {"type": "array"}, "connections": {"type": "object"}, "settings": {"type": "object"}}, "required": ["name", "nodes", "connections"]}},
    {"name": "n8n_update_full_workflow", "desc": "Full workflow update. Requires complete nodes[] and connections{}.", "schema": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "nodes": {"type": "array"}, "connections": {"type": "object"}}, "required": ["id", "nodes", "connections"]}},
    {"name": "n8n_update_partial_workflow", "desc": "Partial update: add/remove/update nodes without full replacement.", "schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "n8n_delete_workflow", "desc": "Permanently delete a workflow by ID.", "schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "n8n_activate_workflow", "desc": "Activate (publish) a workflow so it runs on its trigger.", "schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "n8n_deactivate_workflow", "desc": "Deactivate (unpublish) a workflow.", "schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "n8n_execute_workflow", "desc": "Execute a workflow manually. Returns execution ID.", "schema": {"type": "object", "properties": {"id": {"type": "string"}, "data": {"type": "object"}}, "required": ["id"]}},
    {"name": "n8n_list_executions", "desc": "List workflow executions. Filter by workflowId, status, limit.", "schema": {"type": "object", "properties": {"workflowId": {"type": "string"}, "status": {"type": "string", "enum": ["error", "success", "running", "waiting"]}, "limit": {"type": "integer"}}}},
    {"name": "n8n_get_execution", "desc": "Get a single execution by ID with full node-by-node data.", "schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "n8n_delete_execution", "desc": "Delete an execution record by ID.", "schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "n8n_manage_tags", "desc": "Manage n8n tags. Actions: list, create, update, delete.", "schema": {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "create", "update", "delete"]}}, "required": ["action"]}},
    {"name": "n8n_manage_variables", "desc": "Manage n8n variables. Actions: list, create, update, delete.", "schema": {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "create", "update", "delete"]}}, "required": ["action"]}},
    {"name": "n8n_manage_credentials", "desc": "Manage n8n credentials. Actions: list, get, create, update, delete, getSchema.", "schema": {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "get", "create", "update", "delete", "getSchema"]}}, "required": ["action"]}},
    {"name": "n8n_manage_data_tables", "desc": "Manage n8n data tables (listRows, createTable, etc).", "schema": {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}},
    {"name": "n8n_generate_workflow", "desc": "Generate an n8n workflow from a natural language description using AI.", "schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
    {"name": "n8n_audit_instance", "desc": "Security audit of n8n instance (credentials, DB, nodes, filesystem, webhook risks).", "schema": {"type": "object", "properties": {}}},
]

# ─── Lazy subprocess client ──────────────────────────────────────────────


class _McpClient:
    """Manages one n8n-mcp subprocess and JSON-RPC dispatch.

    Created lazily on first tool call so that asyncio.create_subprocess_exec
    runs inside the application event loop rather than a throwaway loop.
    """

    def __init__(self) -> None:
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._next_id: int = 1
        self._restarts: int = 0

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            return  # already running

        base = os.environ.get("N8N_BASE_URL", "http://localhost:5678").rstrip("/")
        env = os.environ.copy()
        env["N8N_API_URL"] = f"{base}/api/v1"
        env.setdefault("N8N_API_KEY", "")
        env["WEBHOOK_SECURITY_MODE"] = "moderate"  # Allow localhost connections

        start_ts = time.monotonic()
        self.proc = await asyncio.create_subprocess_exec(
            N8N_MCP_BIN,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            # --- MCP initialize handshake ---
            init = await self._rpc_locked("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "jarvis-mcp", "version": "7.0"},
            }, timeout=STARTUP_TIMEOUT_S)

            negotiated = (
                init.get("result", {}).get("protocolVersion")
                or "2024-11-05"
            )

            # "initialized" notification (no JSON-RPC id).
            assert self.proc.stdin is not None
            self.proc.stdin.write(
                json.dumps({
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }).encode() + b"\n"
            )
            await self.proc.stdin.drain()

            elapsed = time.monotonic() - start_ts
            log.info("n8n-mcp subprocess ready (protocol=%s, %.1fs)", negotiated, elapsed)
            self._restarts = 0
        except Exception:
            await self._kill()
            raise

    async def _kill(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        except Exception:
            pass
        finally:
            self.proc = None

    # ── JSON-RPC ───────────────────────────────────────────────────────

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call a tool and return the text content."""
        resp = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        content = resp.get("result", {}).get("content", [])
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", json.dumps(item, ensure_ascii=False))))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts) if parts else json.dumps(resp.get("result", {}))

    async def _rpc(self, method: str, params: dict[str, Any],
                   timeout: float = REQUEST_TIMEOUT_S) -> dict[str, Any]:
        """RPC with auto-start and restart on crash."""
        if self.proc is None or self.proc.returncode is not None:
            if self._restarts >= MAX_RESTARTS:
                raise ConnectionError(
                    f"n8n-mcp dead after {MAX_RESTARTS} restart(s)"
                )
            self._restarts += 1
            log.warning("n8n-mcp restarting (attempt %d/%d)", self._restarts, MAX_RESTARTS)
            await self.start()

        try:
            return await self._rpc_locked(method, params, timeout=timeout)
        except (ConnectionError, TimeoutError, OSError) as exc:
            log.error("n8n-mcp RPC failed: %s", exc)
            await self._kill()
            # One retry after crash.
            if self._restarts < MAX_RESTARTS:
                self._restarts += 1
                await self.start()
                return await self._rpc_locked(method, params, timeout=timeout)
            raise ConnectionError(f"n8n-mcp unreachable: {exc}") from exc

    async def _rpc_locked(self, method: str, params: dict[str, Any],
                          timeout: float) -> dict[str, Any]:
        async with self._lock:
            assert self.proc is not None and self.proc.stdin is not None
            rid = self._next_id
            self._next_id += 1

            req = json.dumps({
                "jsonrpc": "2.0", "id": rid,
                "method": method, "params": params,
            }, ensure_ascii=False)
            self.proc.stdin.write((req + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

            await asyncio.sleep(0.02)

            assert self.proc.stdout is not None
            try:
                line = await asyncio.wait_for(
                    self.proc.stdout.readline(), timeout=timeout
                )
            except asyncio.TimeoutError:
                raise TimeoutError(f"n8n-mcp timeout ({timeout}s) on {method}")

            if not line:
                raise ConnectionError("n8n-mcp closed stdout")

            try:
                raw = line.decode("utf-8", errors="replace").strip()
                resp = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ConnectionError(f"n8n-mcp bad JSON: {raw[:200]}") from e

            if resp.get("id") != rid:
                raise ConnectionError(
                    f"RPC id mismatch: expected {rid}, got {resp.get('id')}"
                )
            if "error" in resp:
                err_obj = resp["error"]
                raise RuntimeError(
                    f"MCP error {err_obj.get('code', '?')}: {err_obj.get('message', '?')}"
                )
            return resp


# ─── Singleton ───────────────────────────────────────────────────────────

_client: Optional[_McpClient] = None


def _get_client() -> _McpClient:
    global _client
    if _client is None:
        _client = _McpClient()
    return _client


# ─── Tool handler factory ────────────────────────────────────────────────

def _make_handler(tool_name: str):
    """Return an async handler that proxies the call to the n8n-mcp subprocess."""
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        require_dict(args, "n8n tool arguments")
        client = _get_client()
        try:
            text = await client.call_tool(tool_name, args)
            return ok(text, server="n8n", tool=tool_name)
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            return err(str(exc), code="n8n_mcp_error", server="n8n", tool=tool_name)
    return handler


# ─── Registration ────────────────────────────────────────────────────────

_registered = False


def _register() -> None:
    global _registered
    if _registered:
        return
    for tdef in _N8N_MCP_TOOLS:
        name = tdef["name"]
        server.tools[name] = Tool(
            name=name,
            description=tdef["desc"],
            handler=_make_handler(name),
            input_schema=tdef["schema"],
        )
    _registered = True


def get_server() -> Server:
    _register()
    return server


# ─── Direct call (used by telegram_bot bridge) ───────────────────────────

async def call_tool(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Direct tool call — used by the Telegram bot dispatch bridge."""
    client = _get_client()
    try:
        text = await client.call_tool(tool_name, args)
        return ok(text, server="n8n", tool=tool_name)
    except Exception as exc:
        return err(str(exc), code="n8n_mcp_error", server="n8n", tool=tool_name)
