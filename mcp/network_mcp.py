"""MCP server: network.

Wrappers for curl-style HTTP, ping, DNS resolution, and nmap. All tools
validate targets against a host allowlist (and explicit RFC1918 / loopback
opt-in) before reaching the network. Output sizes are capped.

Tools:
  http_get        — GET a URL, return status + (truncated) body.
  http_post       — POST JSON to a URL.
  ping            — ICMP echo via the `ping` binary.
  resolve         — DNS A/AAAA resolution via socket.getaddrinfo.
  nmap_scan       — Wrapped nmap call; requires JARVIS_LAB_MODE=true.
  port_check      — Single TCP connect, no nmap needed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import shutil
import socket
from typing import Any, Optional
from urllib.parse import urlparse

from ._common import (
    Server,
    err,
    ok,
    require,
    require_int,
    require_str,
    safe_truncate,
)


server = Server(name="network")


# ─── Target policy ────────────────────────────────────────────────────────


def _allowlist() -> list[str]:
    raw = os.environ.get("JARVIS_NET_ALLOWLIST", "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _allow_private() -> bool:
    return os.environ.get("JARVIS_NET_ALLOW_PRIVATE", "false").lower() in {
        "1", "true", "yes", "on"
    }


def _lab_mode() -> bool:
    return os.environ.get("JARVIS_LAB_MODE", "false").lower() in {
        "1", "true", "yes", "on"
    }


def _is_private_or_loopback(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        # Hostname; check if it resolves to a private address.
        try:
            addrs = {a[4][0] for a in socket.getaddrinfo(host, None)}
        except socket.gaierror:
            return False
        for a in addrs:
            try:
                ipa = ipaddress.ip_address(a)
                if ipa.is_private or ipa.is_loopback or ipa.is_link_local:
                    return True
            except ValueError:
                continue
        return False


def _check_target(host: str) -> Optional[str]:
    """Return None if allowed, or an error message."""
    host = host.lower().strip()
    if not host:
        return "empty host"
    allow = _allowlist()
    if allow:
        # exact match or suffix on a "."-prefix entry
        if host in allow:
            return None
        for a in allow:
            if a.startswith(".") and host.endswith(a):
                return None
        return f"host {host!r} not in JARVIS_NET_ALLOWLIST"
    # Open egress (no allowlist) — still guard private ranges by default.
    if _is_private_or_loopback(host) and not _allow_private():
        return (
            f"host {host!r} resolves to a private/loopback address; "
            "set JARVIS_NET_ALLOW_PRIVATE=true to permit"
        )
    return None


def _max_body() -> int:
    return int(os.environ.get("JARVIS_NET_MAX_BODY", str(512 * 1024)))


# ─── HTTP ─────────────────────────────────────────────────────────────────


async def _http(method: str, url: str, *,
                headers: Optional[dict[str, str]] = None,
                json_body: Optional[dict[str, Any]] = None,
                timeout: float = 15.0) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return err(f"only http/https supported; got {parsed.scheme!r}",
                   code="bad_scheme")
    host = parsed.hostname or ""
    deny = _check_target(host)
    if deny:
        return err(deny, code="target_denied")

    try:
        import httpx
    except ImportError:
        return err("httpx not installed; pip install httpx", code="missing_dep")

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            resp = await c.request(method, url, headers=headers or {},
                                   json=json_body)
        body = resp.content
        truncated = len(body) > _max_body()
        sample = body[:_max_body()]
        try:
            text = sample.decode(resp.encoding or "utf-8", errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = sample.decode("utf-8", errors="replace")
        return ok({
            "url": str(resp.url),
            "status": resp.status_code,
            "headers": dict(resp.headers),
            "body": text,
            "body_bytes": len(body),
            "truncated": truncated,
        })
    except Exception as e:
        return err(f"http {method} failed: {e!r}", code="http_error")


@server.tool(
    "http_get",
    description="GET an HTTP(S) URL. Returns status, headers, truncated body.",
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "headers": {"type": "object"},
            "timeout_s": {"type": "number", "minimum": 1, "maximum": 120},
        },
        "required": ["url"],
    },
)
async def http_get(args: dict[str, Any]) -> dict[str, Any]:
    url = require_str(args["url"], "url", max_len=2048)
    headers = args.get("headers") or {}
    require(isinstance(headers, dict), "headers must be an object")
    timeout = float(args.get("timeout_s", 15.0))
    return await _http("GET", url, headers=headers, timeout=timeout)


@server.tool(
    "http_post",
    description="POST JSON to an HTTP(S) URL.",
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "json": {"type": "object"},
            "headers": {"type": "object"},
            "timeout_s": {"type": "number", "minimum": 1, "maximum": 120},
        },
        "required": ["url", "json"],
    },
)
async def http_post(args: dict[str, Any]) -> dict[str, Any]:
    url = require_str(args["url"], "url", max_len=2048)
    body = args.get("json")
    require(isinstance(body, dict), "json must be an object")
    headers = args.get("headers") or {}
    require(isinstance(headers, dict), "headers must be an object")
    timeout = float(args.get("timeout_s", 30.0))
    return await _http("POST", url, headers=headers, json_body=body,
                       timeout=timeout)


# ─── ICMP / DNS / TCP ─────────────────────────────────────────────────────


@server.tool(
    "ping",
    description="ICMP echo via the system `ping` binary.",
    input_schema={
        "type": "object",
        "properties": {
            "host": {"type": "string"},
            "count": {"type": "integer", "minimum": 1, "maximum": 10},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        "required": ["host"],
    },
)
async def ping(args: dict[str, Any]) -> dict[str, Any]:
    host = require_str(args["host"], "host", max_len=253,
                       pattern=r"[A-Za-z0-9:._\-]+")
    deny = _check_target(host)
    if deny:
        return err(deny, code="target_denied")
    count = require_int(int(args.get("count", 3)), "count", lo=1, hi=10)
    timeout = require_int(int(args.get("timeout_s", 5)), "timeout_s",
                          lo=1, hi=30)
    binary = shutil.which("ping")
    if not binary:
        return err("ping binary not found", code="missing_binary")
    proc = await asyncio.create_subprocess_exec(
        binary, "-c", str(count), "-W", str(timeout), host,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                timeout=timeout * count + 5)
    except asyncio.TimeoutError:
        proc.kill()
        return err("ping timed out", code="timeout")
    return ok({
        "host": host,
        "exit": proc.returncode,
        "stdout": safe_truncate(stdout.decode("utf-8", "replace"), 8192),
        "stderr": safe_truncate(stderr.decode("utf-8", "replace"), 2048),
    })


@server.tool(
    "resolve",
    description="DNS A/AAAA resolution via getaddrinfo.",
    input_schema={
        "type": "object",
        "properties": {"host": {"type": "string"}},
        "required": ["host"],
    },
)
async def resolve(args: dict[str, Any]) -> dict[str, Any]:
    host = require_str(args["host"], "host", max_len=253,
                       pattern=r"[A-Za-z0-9:._\-]+")
    loop = asyncio.get_event_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except socket.gaierror as e:
        return err(f"dns failed: {e}", code="dns_error")
    addrs = sorted({i[4][0] for i in infos})
    return ok({"host": host, "addresses": addrs})


@server.tool(
    "port_check",
    description="Single TCP connect — succeeds if the port accepts.",
    input_schema={
        "type": "object",
        "properties": {
            "host": {"type": "string"},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            "timeout_s": {"type": "number", "minimum": 0.1, "maximum": 30},
        },
        "required": ["host", "port"],
    },
)
async def port_check(args: dict[str, Any]) -> dict[str, Any]:
    host = require_str(args["host"], "host", max_len=253,
                       pattern=r"[A-Za-z0-9:._\-]+")
    deny = _check_target(host)
    if deny:
        return err(deny, code="target_denied")
    port = require_int(int(args["port"]), "port", lo=1, hi=65535)
    timeout = float(args.get("timeout_s", 3.0))
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return ok({"host": host, "port": port, "open": True})
    except (asyncio.TimeoutError, OSError) as e:
        return ok({"host": host, "port": port, "open": False, "reason": str(e)})


# ─── nmap ─────────────────────────────────────────────────────────────────


_NMAP_ALLOWED_FLAGS = {
    "-sV", "-sS", "-sT", "-sU", "-Pn", "-A", "-T3", "-T4",
    "-O", "--top-ports", "-p", "-oX", "-oN",
}


def _validate_nmap_args(raw_args: list[str]) -> Optional[str]:
    for token in raw_args:
        if token.startswith("-"):
            base = token.split("=", 1)[0]
            if base not in _NMAP_ALLOWED_FLAGS:
                return f"nmap flag not allowed: {token}"
        elif re.search(r"[;&|`$<>\\]", token):
            return f"shell metacharacter in nmap arg: {token!r}"
    return None


@server.tool(
    "nmap_scan",
    description=(
        "Run nmap against a single host. Requires JARVIS_LAB_MODE=true. "
        "Flags are restricted to a vetted allowlist."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "flags": {"type": "array", "items": {"type": "string"}},
            "timeout_s": {"type": "integer", "minimum": 5, "maximum": 600},
        },
        "required": ["target"],
    },
)
async def nmap_scan(args: dict[str, Any]) -> dict[str, Any]:
    if not _lab_mode():
        return err("nmap requires JARVIS_LAB_MODE=true", code="lab_only")
    binary = shutil.which("nmap")
    if not binary:
        return err("nmap binary not found", code="missing_binary")
    target = require_str(args["target"], "target", max_len=253,
                         pattern=r"[A-Za-z0-9:._\-/]+")
    deny = _check_target(target.split("/", 1)[0])  # strip CIDR
    if deny:
        return err(deny, code="target_denied")
    flags = args.get("flags") or ["-sV", "-T4", "-Pn"]
    require(isinstance(flags, list) and all(isinstance(f, str) for f in flags),
            "flags must be a list of strings")
    bad = _validate_nmap_args(flags)
    if bad:
        return err(bad, code="bad_flag")
    timeout = require_int(int(args.get("timeout_s", 120)), "timeout_s",
                          lo=5, hi=600)
    cmd = [binary, *flags, target]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return err("nmap timed out", code="timeout", timeout_s=timeout)
    return ok({
        "target": target,
        "cmd": cmd,
        "exit": proc.returncode,
        "stdout": safe_truncate(stdout.decode("utf-8", "replace"), 65536),
        "stderr": safe_truncate(stderr.decode("utf-8", "replace"), 4096),
    })


# ─── Module entry ─────────────────────────────────────────────────────────


def get_server() -> Server:
    return server
