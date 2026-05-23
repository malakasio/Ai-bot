"""MCP server: n8n — hybrid architecture combining stdio bridge + REST client.

ARCHITECTURE:
  - Validation/docs tools → upstream czlonkowski/n8n-mcp stdio bridge (7 tools)
  - Execution history tools → local REST client to n8n API (2 tools)

IMPORTANT: n8n public API does NOT support workflow execution.
  - Use n8n_trigger tool (automation_mcp.py) for webhook-based execution
  - This module only provides execution history queries (list/get executions)

Env:
  N8N_BASE_URL  — n8n instance URL (default: http://localhost:5678)
  N8N_API_KEY   — n8n API key from Settings → n8n API
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import time
from typing import Any, Optional

from ._common import Server, Tool, err, ok, require_dict, require_str, safe_truncate

log = logging.getLogger("jarvis.mcp.n8n")

# ─── Configuration ───────────────────────────────────────────────────────

N8N_MCP_BIN = shutil.which("n8n-mcp") or "n8n-mcp"
STARTUP_TIMEOUT_S = int(os.environ.get("MCP_STARTUP_TIMEOUT", "30"))
REQUEST_TIMEOUT_S = int(os.environ.get("MCP_REQUEST_TIMEOUT", "120"))
MAX_RESTARTS = int(os.environ.get("MCP_MAX_RESTARTS", "2"))

server = Server(name="n8n")

# ─── REST client helpers (for execution tools) ───────────────────────────


def _base_url() -> str:
    return (os.environ.get("N8N_BASE_URL", "").strip().rstrip("/")
            or "http://localhost:5678")


def _api_key() -> str:
    return os.environ.get("N8N_API_KEY", "").strip()


def _headers() -> dict[str, str]:
    h = {
        "User-Agent": "jarvis-mcp/7.0",
        "Content-Type": "application/json",
    }
    key = _api_key()
    if key:
        h["X-N8N-API-KEY"] = key
    return h


async def _n8n_request(method: str, path: str, *,
                       json_body: Optional[dict[str, Any]] = None,
                       timeout: float = 60.0) -> dict[str, Any]:
    """Call the n8n REST API and return a structured response."""
    url = f"{_base_url()}/api/v1{path}"
    try:
        import httpx
    except ImportError:
        return {"_error": "httpx not installed; pip install httpx"}
    try:
        async with httpx.AsyncClient(timeout=timeout,
                                     follow_redirects=True) as c:
            resp = await c.request(method, url,
                                   headers=_headers(),
                                   json=json_body)
        body = resp.text
        try:
            data = json.loads(body)
        except Exception:
            data = {"raw": safe_truncate(body, 32 * 1024)}
        return {
            "ok": 200 <= resp.status_code < 300,
            "status": resp.status_code,
            "headers": dict(resp.headers),
            "data": data,
            "body_bytes": len(resp.content),
            "url": str(resp.url),
        }
    except Exception as e:
        return {"_error": repr(e)}


def _check_config() -> Optional[str]:
    """Return error message if config is missing, else None."""
    if not _api_key():
        return "N8N_API_KEY not set"
    return None


# ─── Tool definitions ────────────────────────────────────────────────────
#
# UPSTREAM TOOLS (7): Provided by czlonkowski/n8n-mcp stdio bridge
# These are documentation/validation tools only.

_UPSTREAM_TOOLS: list[dict[str, Any]] = [
    {"name": "tools_documentation", "desc": "Get documentation for all available n8n-mcp tools.", "schema": {"type": "object", "properties": {}}},
    {"name": "search_nodes", "desc": "Search n8n node documentation by keyword.", "schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_node", "desc": "Get detailed documentation for a specific n8n node.", "schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "validate_node", "desc": "Validate a node configuration against its schema.", "schema": {"type": "object", "properties": {"node": {"type": "object"}}, "required": ["node"]}},
    {"name": "get_template", "desc": "Get an n8n workflow template by ID.", "schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "search_templates", "desc": "Search n8n workflow templates.", "schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "validate_workflow", "desc": "Validate a workflow structure.", "schema": {"type": "object", "properties": {"workflow": {"type": "object"}}, "required": ["workflow"]}},
]

# LOCAL REST TOOLS (5): Provided by local REST client
# These query execution history, workflows, create workflows, and manage credentials.
# Note: Workflow execution is NOT supported by n8n public API.
# Use n8n_trigger tool (automation_mcp.py) for webhook-based execution instead.

_LOCAL_REST_TOOLS: list[dict[str, Any]] = [
    {"name": "n8n_list_executions", "desc": "List workflow executions. Filter by workflow_id, status, limit.", "schema": {"type": "object", "properties": {"workflow_id": {"type": "string"}, "status": {"type": "string", "enum": ["error", "success", "running", "waiting"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}}},
    {"name": "n8n_get_execution", "desc": "Get a single execution by ID with full node-by-node data.", "schema": {"type": "object", "properties": {"execution_id": {"type": "string"}}, "required": ["execution_id"]}},
    {"name": "n8n_list_workflows", "desc": "List all workflows with optional filters (active, tags, name search).", "schema": {"type": "object", "properties": {"active": {"type": "boolean", "description": "Filter by active status"}, "tags": {"type": "string", "description": "Comma-separated tag names"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Max workflows to return (default: 100)"}}}},
    {"name": "n8n_create_workflow", "desc": "Create a new n8n workflow. Automatically links Telegram credentials if workflow contains Telegram nodes. Returns workflow ID and metadata. Note: Workflow will be created as inactive - activate manually in n8n UI.", "schema": {"type": "object", "properties": {"name": {"type": "string", "description": "Workflow name"}, "nodes": {"type": "array", "description": "Array of workflow nodes"}, "connections": {"type": "object", "description": "Node connections object"}, "settings": {"type": "object", "description": "Workflow settings (optional)"}, "telegram_bot_token": {"type": "string", "description": "Telegram bot token (optional, for auto-creating credential)"}}, "required": ["name", "nodes", "connections"]}},
    {"name": "n8n_create_credential", "desc": "Create a Telegram API credential in n8n. Returns credential ID. Required for Telegram nodes in workflows.", "schema": {"type": "object", "properties": {"name": {"type": "string", "description": "Credential name"}, "bot_token": {"type": "string", "description": "Telegram bot token from @BotFather"}}, "required": ["name", "bot_token"]}},
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


# ─── REST tool implementations ───────────────────────────────────────────


async def _rest_list_executions(args: dict[str, Any]) -> dict[str, Any]:
    """List executions via REST API."""
    err_msg = _check_config()
    if err_msg:
        return err(err_msg, code="missing_config")

    limit = args.get("limit", 100)
    if limit is not None:
        limit = int(limit)
        if limit < 1 or limit > 100:
            return err("limit must be 1-100", code="invalid_input")

    wf_id = args.get("workflow_id")
    status = args.get("status")

    params = []
    if limit:
        params.append(f"take={limit}")
    if wf_id:
        wf_id = require_str(str(wf_id), "workflow_id", max_len=64,
                            pattern=r"[A-Za-z0-9]+")
        params.append(f"workflowId={wf_id}")
    if status:
        params.append(f"status={status}")
    qs = "?" + "&".join(params) if params else ""

    resp = await _n8n_request("GET", f"/executions{qs}")
    if "_error" in resp:
        return err(f"n8n request failed: {resp['_error']}", code="http_error")
    if not resp["ok"]:
        return err(f"n8n returned HTTP {resp['status']}", code="http_status",
                   detail=resp["data"])

    data = resp["data"]
    executions = data.get("data", data) if isinstance(data, dict) else data
    return ok({
        "count": len(executions) if isinstance(executions, list) else 0,
        "executions": executions,
    })


async def _rest_get_execution(args: dict[str, Any]) -> dict[str, Any]:
    """Get execution details via REST API."""
    err_msg = _check_config()
    if err_msg:
        return err(err_msg, code="missing_config")

    exec_id = require_str(args["execution_id"], "execution_id", max_len=64,
                          pattern=r"[0-9]+")

    resp = await _n8n_request("GET", f"/executions/{exec_id}")
    if "_error" in resp:
        return err(f"n8n request failed: {resp['_error']}", code="http_error")
    if not resp["ok"]:
        return err(f"n8n returned HTTP {resp['status']}", code="http_status",
                   detail=resp["data"])

    return ok(resp["data"])


async def _rest_list_workflows(args: dict[str, Any]) -> dict[str, Any]:
    """List workflows via REST API."""
    err_msg = _check_config()
    if err_msg:
        return err(err_msg, code="missing_config")

    limit = args.get("limit", 100)
    if limit is not None:
        limit = int(limit)
        if limit < 1 or limit > 100:
            return err("limit must be 1-100", code="invalid_input")

    active = args.get("active")
    tags = args.get("tags")

    params = []
    if limit:
        params.append(f"limit={limit}")
    if active is not None:
        params.append(f"active={'true' if active else 'false'}")
    if tags:
        # Validate tags (alphanumeric + hyphen/underscore only)
        tag_list = [t.strip() for t in tags.split(",")]
        for tag in tag_list:
            if not re.match(r'^[A-Za-z0-9_-]+$', tag):
                return err(f"Invalid tag format: {tag}", code="invalid_input")
        params.append(f"tags={tags}")
    qs = "?" + "&".join(params) if params else ""

    resp = await _n8n_request("GET", f"/workflows{qs}")
    if "_error" in resp:
        return err(f"n8n request failed: {resp['_error']}", code="http_error")
    if not resp["ok"]:
        return err(f"n8n returned HTTP {resp['status']}", code="http_status",
                   detail=resp["data"])

    data = resp["data"]
    workflows = data.get("data", data) if isinstance(data, dict) else data
    return ok({
        "count": len(workflows) if isinstance(workflows, list) else 0,
        "workflows": workflows,
    })


async def _rest_create_workflow(args: dict[str, Any]) -> dict[str, Any]:
    """Create workflow via REST API with automatic Telegram credential linking."""
    err_msg = _check_config()
    if err_msg:
        return err(err_msg, code="missing_config")

    # Validate required fields
    name = require_str(args["name"], "name", max_len=128)
    nodes = args.get("nodes", [])
    connections = args.get("connections", {})
    settings = args.get("settings", {})
    telegram_bot_token = args.get("telegram_bot_token")

    if not isinstance(nodes, list) or len(nodes) == 0:
        return err("nodes must be a non-empty array", code="invalid_input")
    if not isinstance(connections, dict):
        return err("connections must be an object", code="invalid_input")
    if not isinstance(settings, dict):
        return err("settings must be an object", code="invalid_input")

    # Check if workflow has Telegram nodes and auto-link credentials
    telegram_credential_id = None
    has_telegram_nodes = any(
        node.get("type") == "n8n-nodes-base.telegram"
        for node in nodes
    )

    if has_telegram_nodes:
        # Try to get existing Telegram credential or create new one
        if telegram_bot_token:
            # Create new credential with provided token
            cred_result = await _rest_create_credential({
                "name": f"Telegram Bot - {name}",
                "bot_token": telegram_bot_token
            })
            if cred_result.get("ok"):
                telegram_credential_id = cred_result["data"].get("id")
        else:
            # Try to find existing Telegram credential
            creds_resp = await _n8n_request("GET", "/credentials?type=telegramApi")
            if creds_resp.get("ok"):
                creds_data = creds_resp["data"]
                creds = creds_data.get("data", creds_data) if isinstance(creds_data, dict) else creds_data
                if isinstance(creds, list) and len(creds) > 0:
                    telegram_credential_id = creds[0].get("id")

        # Link credential to Telegram nodes
        if telegram_credential_id:
            for node in nodes:
                if node.get("type") == "n8n-nodes-base.telegram":
                    if "credentials" not in node:
                        node["credentials"] = {}
                    node["credentials"]["telegramApi"] = {
                        "id": telegram_credential_id,
                        "name": f"Telegram Bot - {name}"
                    }

    # Build workflow payload
    payload = {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "settings": settings
    }

    resp = await _n8n_request("POST", "/workflows", json_body=payload)
    if "_error" in resp:
        return err(f"n8n request failed: {resp['_error']}", code="http_error")
    if not resp["ok"]:
        return err(f"n8n returned HTTP {resp['status']}", code="http_status",
                   detail=resp["data"])

    data = resp["data"]
    return ok({
        "id": data.get("id"),
        "name": data.get("name"),
        "active": data.get("active", False),
        "created": data.get("createdAt"),
        "url": f"{_base_url()}/workflow/{data.get('id')}",
        "note": "Workflow created as inactive. Activate manually in n8n UI or via n8n internal API."
    })


async def _rest_create_credential(args: dict[str, Any]) -> dict[str, Any]:
    """Create Telegram API credential via REST API."""
    err_msg = _check_config()
    if err_msg:
        return err(err_msg, code="missing_config")

    # Validate required fields
    name = require_str(args["name"], "name", max_len=128)
    bot_token = require_str(args["bot_token"], "bot_token", max_len=256)

    # Build credential payload for Telegram API
    payload = {
        "name": name,
        "type": "telegramApi",
        "data": {
            "accessToken": bot_token
        }
    }

    resp = await _n8n_request("POST", "/credentials", json_body=payload)
    if "_error" in resp:
        return err(f"n8n request failed: {resp['_error']}", code="http_error")
    if not resp["ok"]:
        return err(f"n8n returned HTTP {resp['status']}", code="http_status",
                   detail=resp["data"])

    data = resp["data"]
    return ok({
        "id": data.get("id"),
        "name": data.get("name"),
        "type": data.get("type"),
        "created": data.get("createdAt")
    })


# ─── Tool handler factory ────────────────────────────────────────────────

def _make_handler(tool_name: str):
    """Return an async handler that routes to either REST or stdio bridge."""
    # Route execution history, workflow, and credential tools to local REST client
    if tool_name == "n8n_list_executions":
        return _rest_list_executions
    elif tool_name == "n8n_get_execution":
        return _rest_get_execution
    elif tool_name == "n8n_list_workflows":
        return _rest_list_workflows
    elif tool_name == "n8n_create_workflow":
        return _rest_create_workflow
    elif tool_name == "n8n_create_credential":
        return _rest_create_credential
        return _rest_create_workflow

    # Route all other tools to upstream stdio bridge
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

    # Register upstream stdio bridge tools
    for tdef in _UPSTREAM_TOOLS:
        name = tdef["name"]
        server.tools[name] = Tool(
            name=name,
            description=tdef["desc"],
            handler=_make_handler(name),
            input_schema=tdef["schema"],
        )

    # Register local REST execution tools
    for tdef in _LOCAL_REST_TOOLS:
        name = tdef["name"]
        server.tools[name] = Tool(
            name=name,
            description=tdef["desc"],
            handler=_make_handler(name),
            input_schema=tdef["schema"],
        )

    _registered = True
    log.info("Registered %d upstream + %d local REST n8n tools",
             len(_UPSTREAM_TOOLS), len(_LOCAL_REST_TOOLS))


def get_server() -> Server:
    _register()
    return server


# ─── Direct call (used by telegram_bot bridge) ───────────────────────────

async def call_tool(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Direct tool call — used by the Telegram bot dispatch bridge.

    Routes to appropriate backend (REST or stdio bridge).
    """
    _register()  # Ensure tools are registered

    # Route execution history, workflow, and credential tools to REST handlers
    if tool_name == "n8n_list_executions":
        return await _rest_list_executions(args)
    elif tool_name == "n8n_get_execution":
        return await _rest_get_execution(args)
    elif tool_name == "n8n_list_workflows":
        return await _rest_list_workflows(args)
    elif tool_name == "n8n_create_workflow":
        return await _rest_create_workflow(args)
    elif tool_name == "n8n_create_credential":
        return await _rest_create_credential(args)

    # Route all other tools to stdio bridge
    client = _get_client()
    try:
        text = await client.call_tool(tool_name, args)
        return ok(text, server="n8n", tool=tool_name)
    except Exception as exc:
        return err(str(exc), code="n8n_mcp_error", server="n8n", tool=tool_name)
