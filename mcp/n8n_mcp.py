"""MCP server: n8n — full workflow automation via n8n REST API.

Provides create, list, get, update, delete, activate/deactivate, and
trigger workflows. Also lists and retrieves executions.

Credentials sourced from env:
  N8N_BASE_URL  — n8n instance base URL (e.g. http://localhost:5678)
  N8N_API_KEY   — n8n API key (generated in Settings → n8n API)
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Optional

from ._common import (
    Server,
    err,
    ok,
    require,
    require_dict,
    require_int,
    require_str,
    safe_truncate,
)


server = Server(name="n8n")


# ─── Helpers ────────────────────────────────────────────────────────────────


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
        return err("httpx not installed; pip install httpx",
                   code="missing_dep")
    try:
        async with httpx.AsyncClient(timeout=timeout,
                                     follow_redirects=True) as c:
            resp = await c.request(method, url,
                                   headers=_headers(),
                                   json=json_body)
        body = resp.text
        import json as _json
        try:
            data = _json.loads(body)
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


# ─── id/path validators ──────────────────────────────────────────────────────


_ID_RE = re.compile(r"^[A-Za-z0-9]+$")
_WORKFLOW_PATH_RE = re.compile(r"^[A-Za-z0-9_\-/]{1,256}$")


# ─── Tools ───────────────────────────────────────────────────────────────────


@server.tool(
    "n8n_list_workflows",
    description=(
        "List all n8n workflows. Returns id, name, active status, "
        "created/updated timestamps for each workflow."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "active_only": {"type": "boolean"},
        },
        "required": [],
    },
)
async def n8n_list_workflows(args: dict[str, Any]) -> dict[str, Any]:
    err_msg = _check_config()
    if err_msg:
        return err(err_msg, code="missing_config")
    limit = args.get("limit", 100)
    if limit is not None:
        limit = require_int(int(limit), "limit", lo=1, hi=100)
    active_only = bool(args.get("active_only", False))
    params = []
    if limit:
        params.append(f"take={limit}")
    if active_only:
        params.append("filter=active=true")
    qs = "?" + "&".join(params) if params else ""
    resp = await _n8n_request("GET", f"/workflows{qs}")
    if "_error" in resp:
        return err(f"n8n request failed: {resp['_error']}", code="http_error")
    data = resp["data"]
    if not resp["ok"]:
        return err(f"n8n returned HTTP {resp['status']}", code="http_status",
                   detail=data)
    workflows = data.get("data", data) if isinstance(data, dict) else data
    return ok({
        "count": len(workflows) if isinstance(workflows, list) else 0,
        "workflows": workflows,
    })


@server.tool(
    "n8n_get_workflow",
    description=(
        "Get a single n8n workflow by ID. Returns the full workflow JSON "
        "including nodes and connections."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string"},
        },
        "required": ["workflow_id"],
    },
)
async def n8n_get_workflow(args: dict[str, Any]) -> dict[str, Any]:
    err_msg = _check_config()
    if err_msg:
        return err(err_msg, code="missing_config")
    wf_id = require_str(args["workflow_id"], "workflow_id", max_len=64,
                        pattern=r"[A-Za-z0-9]+")
    resp = await _n8n_request("GET", f"/workflows/{wf_id}")
    if "_error" in resp:
        return err(f"n8n request failed: {resp['_error']}", code="http_error")
    if not resp["ok"]:
        return err(f"n8n returned HTTP {resp['status']}", code="http_status",
                   detail=resp["data"])
    return ok(resp["data"])


@server.tool(
    "n8n_create_workflow",
    description=(
        "Create a new n8n workflow. Provide name, nodes (array), and "
        "connections (object). Returns the created workflow with its ID."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "nodes": {"type": "array"},
            "connections": {"type": "object"},
            "settings": {"type": "object"},
            "active": {"type": "boolean"},
        },
        "required": ["name", "nodes", "connections"],
    },
)
async def n8n_create_workflow(args: dict[str, Any]) -> dict[str, Any]:
    err_msg = _check_config()
    if err_msg:
        return err(err_msg, code="missing_config")
    name = require_str(args["name"], "name", max_len=256)
    nodes = args.get("nodes", [])
    connections = args.get("connections", {})
    require(isinstance(nodes, list), "nodes must be a list")
    require(isinstance(connections, dict), "connections must be an object")
    settings = args.get("settings", None)
    active = args.get("active", False)

    body = {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "settings": settings or {},
        "active": bool(active),
    }
    resp = await _n8n_request("POST", "/workflows", json_body=body)
    if "_error" in resp:
        return err(f"n8n request failed: {resp['_error']}", code="http_error")
    if not resp["ok"]:
        return err(f"n8n returned HTTP {resp['status']}", code="http_status",
                   detail=resp["data"])
    return ok(resp["data"])


@server.tool(
    "n8n_update_workflow",
    description=(
        "Update an existing n8n workflow by ID. Provide any combination "
        "of name, nodes, connections, settings, or active."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string"},
            "name": {"type": "string"},
            "nodes": {"type": "array"},
            "connections": {"type": "object"},
            "settings": {"type": "object"},
            "active": {"type": "boolean"},
        },
        "required": ["workflow_id"],
    },
)
async def n8n_update_workflow(args: dict[str, Any]) -> dict[str, Any]:
    err_msg = _check_config()
    if err_msg:
        return err(err_msg, code="missing_config")
    wf_id = require_str(args["workflow_id"], "workflow_id", max_len=64,
                        pattern=r"[A-Za-z0-9]+")

    # First fetch existing workflow so we can merge
    get_resp = await _n8n_request("GET", f"/workflows/{wf_id}")
    if "_error" in get_resp:
        return err(f"n8n get failed: {get_resp['_error']}", code="http_error")
    if not get_resp["ok"]:
        return err(f"n8n returned HTTP {get_resp['status']}", code="http_status",
                   detail=get_resp["data"])

    existing = get_resp["data"]
    if isinstance(existing, dict) and "data" in existing:
        existing = existing["data"]
    if isinstance(existing, dict):
        # Build patch body
        body = {
            "id": wf_id,
            "name": str(args.get("name")) if args.get("name") is not None
                   else existing.get("name", "Untitled"),
            "nodes": args.get("nodes") if args.get("nodes") is not None
                     else existing.get("nodes", []),
            "connections": args.get("connections")
                           if args.get("connections") is not None
                           else existing.get("connections", {}),
            "settings": args.get("settings") if args.get("settings") is not None
                        else existing.get("settings", {}),
        }
        if "active" in args:
            body["active"] = bool(args["active"])
        else:
            body["active"] = existing.get("active", False)
    else:
        return err("could not retrieve existing workflow", code="internal_error")

    resp = await _n8n_request("PUT", f"/workflows/{wf_id}", json_body=body)
    if "_error" in resp:
        return err(f"n8n request failed: {resp['_error']}", code="http_error")
    if not resp["ok"]:
        return err(f"n8n returned HTTP {resp['status']}", code="http_status",
                   detail=resp["data"])
    return ok(resp["data"])


@server.tool(
    "n8n_delete_workflow",
    description="Delete an n8n workflow by ID. Returns confirmation.",
    input_schema={
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string"},
        },
        "required": ["workflow_id"],
    },
)
async def n8n_delete_workflow(args: dict[str, Any]) -> dict[str, Any]:
    err_msg = _check_config()
    if err_msg:
        return err(err_msg, code="missing_config")
    wf_id = require_str(args["workflow_id"], "workflow_id", max_len=64,
                        pattern=r"[A-Za-z0-9]+")
    resp = await _n8n_request("DELETE", f"/workflows/{wf_id}")
    if "_error" in resp:
        return err(f"n8n request failed: {resp['_error']}", code="http_error")
    if not resp["ok"]:
        return err(f"n8n returned HTTP {resp['status']}", code="http_status",
                   detail=resp["data"])
    return ok({"deleted": True, "workflow_id": wf_id, "detail": resp["data"]})


@server.tool(
    "n8n_activate_workflow",
    description=(
        "Activate an n8n workflow so it starts listening for triggers."
        "Pass active=true to activate, active=false to deactivate."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string"},
            "active": {"type": "boolean"},
        },
        "required": ["workflow_id", "active"],
    },
)
async def n8n_activate_workflow(args: dict[str, Any]) -> dict[str, Any]:
    err_msg = _check_config()
    if err_msg:
        return err(err_msg, code="missing_config")
    wf_id = require_str(args["workflow_id"], "workflow_id", max_len=64,
                        pattern=r"[A-Za-z0-9]+")
    active = bool(args.get("active", True))

    # n8n's REST API expects method-specific approach for activation:
    # PATCH /workflows/{id} with {"active": true/false}
    resp = await _n8n_request("PATCH", f"/workflows/{wf_id}",
                              json_body={"id": wf_id, "active": active})
    if "_error" in resp:
        return err(f"n8n request failed: {resp['_error']}", code="http_error")
    if not resp["ok"]:
        return err(f"n8n returned HTTP {resp['status']}", code="http_status",
                   detail=resp["data"])
    state = "activated" if active else "deactivated"
    return ok({"workflow_id": wf_id, "active": active,
               "message": f"Workflow {wf_id} {state}"})


@server.tool(
    "n8n_execute_workflow",
    description=(
        "Manually execute (trigger) an n8n workflow. Optionally supply "
        "input data as a JSON object. Returns the execution ID."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string"},
            "data": {"type": "object"},
            "timeout_s": {"type": "number", "minimum": 1, "maximum": 300},
        },
        "required": ["workflow_id"],
    },
)
async def n8n_execute_workflow(args: dict[str, Any]) -> dict[str, Any]:
    err_msg = _check_config()
    if err_msg:
        return err(err_msg, code="missing_config")
    wf_id = require_str(args["workflow_id"], "workflow_id", max_len=64,
                        pattern=r"[A-Za-z0-9]+")
    data = args.get("data", None)
    if data is not None:
        require(isinstance(data, dict), "data must be an object")
    timeout = float(args.get("timeout_s", 120))
    body = {"workflowData": {"id": wf_id}}
    if data:
        body["data"] = data

    resp = await _n8n_request("POST", f"/workflows/{wf_id}/execute",
                              json_body=body, timeout=timeout + 5)
    if "_error" in resp:
        return err(f"n8n request failed: {resp['_error']}", code="http_error")
    if not resp["ok"]:
        return err(f"n8n returned HTTP {resp['status']}", code="http_status",
                   detail=resp["data"])
    result = resp["data"]
    exec_id = None
    if isinstance(result, dict):
        exec_id = result.get("executionId") or result.get("id")
    return ok({
        "workflow_id": wf_id,
        "execution_id": exec_id,
        "detail": result,
    })


@server.tool(
    "n8n_list_executions",
    description=(
        "List execution history for n8n. Optionally filter by workflow_id "
        "or status (error, success, running, waiting)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string"},
            "status": {"type": "string",
                       "enum": ["error", "success", "running", "waiting"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": [],
    },
)
async def n8n_list_executions(args: dict[str, Any]) -> dict[str, Any]:
    err_msg = _check_config()
    if err_msg:
        return err(err_msg, code="missing_config")
    limit = args.get("limit", 100)
    if limit is not None:
        limit = require_int(int(limit), "limit", lo=1, hi=100)
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


@server.tool(
    "n8n_get_execution",
    description=(
        "Get details of a single n8n execution by ID. Returns the full "
        "execution data including node outcomes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "execution_id": {"type": "string"},
        },
        "required": ["execution_id"],
    },
)
async def n8n_get_execution(args: dict[str, Any]) -> dict[str, Any]:
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


# ─── Module entry ─────────────────────────────────────────────────────────


def get_server() -> Server:
    return server
