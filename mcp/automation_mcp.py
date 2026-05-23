"""MCP server: automation.

Adapters for two external automation surfaces:
  * n8n  — fire-and-forget webhook triggers.
  * Apify — run an actor and (optionally) poll for the dataset.

Credentials are sourced from env:
  N8N_WEBHOOK_BASE     — base URL for n8n webhooks (e.g. https://n8n.example/webhook)
  N8N_AUTH_HEADER      — optional auth header value (e.g. "Bearer …")
  APIFY_TOKEN          — Apify API token
  APIFY_BASE           — defaults to https://api.apify.com/v2
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from ._common import (
    Server,
    err,
    ok,
    require,
    require_int,
    require_str,
    safe_truncate,
)


server = Server(name="automation")


# ─── Shared HTTP helper ───────────────────────────────────────────────────


async def _http_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    json_body: Optional[dict[str, Any]] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    try:
        import httpx
    except ImportError:
        return err("httpx not installed; pip install httpx", code="missing_dep")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            resp = await c.request(method, url, headers=headers or {}, json=json_body)
        body = resp.text
        return {
            "status": resp.status_code,
            "headers": dict(resp.headers),
            "body": safe_truncate(body, 32 * 1024),
            "body_bytes": len(resp.content),
            "url": str(resp.url),
        }
    except Exception as e:
        return {"_error": repr(e)}


# ─── n8n ──────────────────────────────────────────────────────────────────


_WEBHOOK_PATH_RE = re.compile(r"^[A-Za-z0-9_\-/]{1,256}$")


@server.tool(
    "n8n_trigger",
    description=(
        "Trigger an n8n webhook by path. Returns the webhook's response "
        "verbatim. Requires N8N_WEBHOOK_BASE."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "payload": {"type": "object"},
            "method": {"type": "string", "enum": ["POST", "GET"]},
            "timeout_s": {"type": "number", "minimum": 1, "maximum": 120},
        },
        "required": ["path"],
    },
)
async def n8n_trigger(args: dict[str, Any]) -> dict[str, Any]:
    base = os.environ.get("N8N_WEBHOOK_BASE", "").rstrip("/")
    if not base:
        return err("N8N_WEBHOOK_BASE not set", code="missing_config")
    path = require_str(args["path"], "path", max_len=256)
    if not _WEBHOOK_PATH_RE.match(path):
        return err("path contains invalid characters", code="invalid_input")
    method = args.get("method", "POST")
    require(method in {"POST", "GET"}, "method must be POST or GET")
    payload = args.get("payload") or {}
    require(isinstance(payload, dict), "payload must be an object")
    timeout = float(args.get("timeout_s", 30.0))

    headers: dict[str, str] = {"User-Agent": "jarvis-mcp/7.0"}
    auth = os.environ.get("N8N_AUTH_HEADER", "").strip()
    if auth:
        headers["Authorization"] = auth

    url = f"{base}/{path.lstrip('/')}"
    resp = await _http_request(
        method,
        url,
        headers=headers,
        json_body=payload if method == "POST" else None,
        timeout=timeout,
    )
    if "_error" in resp:
        return err(f"n8n request failed: {resp['_error']}", code="http_error")
    if not (200 <= resp["status"] < 300):
        return err(f"n8n returned HTTP {resp['status']}", code="http_status", **resp)
    return ok(resp)


# ─── Apify ────────────────────────────────────────────────────────────────


_ACTOR_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)?$")


def _apify_base() -> str:
    return os.environ.get("APIFY_BASE", "https://api.apify.com/v2").rstrip("/")


def _apify_token() -> Optional[str]:
    return os.environ.get("APIFY_TOKEN", "").strip() or None


def _apify_headers() -> dict[str, str]:
    token = _apify_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


@server.tool(
    "apify_run_actor",
    description=(
        "Start an Apify actor run with the given input. Returns the run id "
        "and metadata. Set wait=true to block until the run finishes "
        "(up to wait_timeout_s)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "actor_id": {"type": "string"},
            "input": {"type": "object"},
            "wait": {"type": "boolean"},
            "wait_timeout_s": {"type": "integer", "minimum": 1, "maximum": 600},
            "memory_mb": {"type": "integer", "minimum": 128, "maximum": 16384},
        },
        "required": ["actor_id"],
    },
)
async def apify_run_actor(args: dict[str, Any]) -> dict[str, Any]:
    if not _apify_token():
        return err("APIFY_TOKEN not set", code="missing_config")
    actor_id = require_str(args["actor_id"], "actor_id", max_len=128)
    if not _ACTOR_ID_RE.match(actor_id):
        return err("actor_id has invalid characters", code="invalid_input")
    actor_id_url = actor_id.replace("/", "~")
    actor_input = args.get("input") or {}
    require(isinstance(actor_input, dict), "input must be an object")
    wait = bool(args.get("wait", False))
    wait_timeout = require_int(int(args.get("wait_timeout_s", 120)), "wait_timeout_s", lo=1, hi=600)
    memory_mb = args.get("memory_mb")
    if memory_mb is not None:
        memory_mb = require_int(int(memory_mb), "memory_mb", lo=128, hi=16384)

    base = _apify_base()
    params = []
    if wait:
        params.append(f"waitForFinish={wait_timeout}")
    if memory_mb:
        params.append(f"memory={memory_mb}")
    qs = ("?" + "&".join(params)) if params else ""
    url = f"{base}/acts/{actor_id_url}/runs{qs}"

    resp = await _http_request(
        "POST",
        url,
        headers=_apify_headers(),
        json_body=actor_input,
        timeout=float(wait_timeout + 5),
    )
    if "_error" in resp:
        return err(f"apify request failed: {resp['_error']}", code="http_error")
    if not (200 <= resp["status"] < 300):
        return err(f"apify returned HTTP {resp['status']}", code="http_status", **resp)
    # The Apify API returns {"data": {...run...}} on success.
    import json as _json

    try:
        body = _json.loads(resp["body"])
    except Exception:
        body = {"raw": resp["body"]}
    return ok({"status": resp["status"], "run": body.get("data", body)})


@server.tool(
    "apify_get_dataset",
    description="Fetch items from an Apify dataset.",
    input_schema={
        "type": "object",
        "properties": {
            "dataset_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "offset": {"type": "integer", "minimum": 0},
        },
        "required": ["dataset_id"],
    },
)
async def apify_get_dataset(args: dict[str, Any]) -> dict[str, Any]:
    if not _apify_token():
        return err("APIFY_TOKEN not set", code="missing_config")
    dataset_id = require_str(
        args["dataset_id"], "dataset_id", max_len=64, pattern=r"[A-Za-z0-9_\-]+"
    )
    limit = require_int(int(args.get("limit", 100)), "limit", lo=1, hi=1000)
    offset = require_int(int(args.get("offset", 0)), "offset", lo=0, hi=10_000_000)
    base = _apify_base()
    url = f"{base}/datasets/{dataset_id}/items?limit={limit}&offset={offset}&format=json"
    resp = await _http_request("GET", url, headers=_apify_headers(), timeout=30.0)
    if "_error" in resp:
        return err(f"apify request failed: {resp['_error']}", code="http_error")
    if not (200 <= resp["status"] < 300):
        return err(f"apify returned HTTP {resp['status']}", code="http_status", **resp)
    import json as _json

    try:
        items = _json.loads(resp["body"])
    except Exception:
        items = []
    return ok(
        {
            "dataset_id": dataset_id,
            "limit": limit,
            "offset": offset,
            "count": len(items) if isinstance(items, list) else 0,
            "items": items,
        }
    )


# ─── Module entry ─────────────────────────────────────────────────────────


def get_server() -> Server:
    return server
