"""MCP server: computer-use (browser automation).

Playwright-based browser automation for DOM interaction, navigation, and
screenshots. All URLs are validated against security zones before navigation.
Runs in headless mode with anti-detection stealth patches.

Tools:
  browser_navigate   — Navigate to URL, wait for load/networkidle.
  browser_click      — Click element by CSS selector.
  browser_type       — Type text into element by CSS selector.
  browser_screenshot — Capture screenshot (full page or element).

Security:
  - URL allowlist enforcement via JARVIS_NET_ALLOWLIST
  - Private/loopback URLs blocked unless JARVIS_NET_ALLOW_PRIVATE=true
  - Selector sanitization (rejects javascript:, data: schemes)
  - 30s timeout per action
  - Headless mode enforced
  - Download blocker enabled
  - Rate limiting: max 10 actions/min (enforced by agent.py)
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeout
from playwright_stealth import stealth_async

from ._common import (
    Server,
    err,
    ok,
    require,
    require_str,
    require_in,
    safe_truncate,
)


server = Server(name="computer-use")

# Singleton browser instance (lazy-initialized)
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None


# ─── Security zone validation ─────────────────────────────────────────────


def _allowlist() -> list[str]:
    raw = os.environ.get("JARVIS_NET_ALLOWLIST", "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _allow_private() -> bool:
    return os.environ.get("JARVIS_NET_ALLOW_PRIVATE", "false").lower() in {
        "1", "true", "yes", "on"
    }


def _is_private_or_loopback(host: str) -> bool:
    import ipaddress
    import socket
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        # Hostname; resolve and check
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


def _check_url(url: str) -> Optional[str]:
    """Return None if allowed, or an error message."""
    url = url.strip()
    if not url:
        return "empty URL"

    # Block dangerous schemes
    parsed = urlparse(url)
    if parsed.scheme in {"javascript", "data", "file", "vbscript"}:
        return f"blocked scheme: {parsed.scheme}"

    if parsed.scheme not in {"http", "https"}:
        return f"unsupported scheme: {parsed.scheme} (only http/https allowed)"

    host = parsed.hostname
    if not host:
        return "URL has no hostname"

    host = host.lower()
    allow = _allowlist()

    if allow:
        # Exact match or suffix on a "."-prefix entry
        if host in allow:
            return None
        for a in allow:
            if a.startswith(".") and host.endswith(a):
                return None
        return f"host {host!r} not in JARVIS_NET_ALLOWLIST"

    # Open egress (no allowlist) — still guard private ranges by default
    if _is_private_or_loopback(host) and not _allow_private():
        return (
            f"host {host!r} resolves to a private/loopback address; "
            "set JARVIS_NET_ALLOW_PRIVATE=true to permit"
        )

    return None


def _sanitize_selector(selector: str) -> Optional[str]:
    """Return None if safe, or an error message."""
    selector = selector.strip()
    if not selector:
        return "empty selector"

    # Block dangerous patterns
    dangerous = ["javascript:", "data:", "vbscript:", "<script", "onerror=", "onload="]
    for pattern in dangerous:
        if pattern in selector.lower():
            return f"blocked pattern in selector: {pattern}"

    return None


# ─── Browser lifecycle ────────────────────────────────────────────────────


async def _ensure_browser() -> tuple[Browser, BrowserContext, Page]:
    """Lazy-initialize browser, context, and page. Reuse across calls."""
    global _browser, _context, _page

    if _browser is None or not _browser.is_connected():
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        _context = await _browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/New_York",
            permissions=[],  # No permissions by default
            accept_downloads=False,  # Block downloads
        )
        _page = await _context.new_page()
        await stealth_async(_page)  # Apply anti-detection patches

    return _browser, _context, _page


# ─── Tools ────────────────────────────────────────────────────────────────


@server.tool(
    name="browser_navigate",
    description="Navigate browser to URL and wait for page load",
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to navigate to (http/https only)"},
            "wait_for": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle"],
                "description": "Wait condition (default: load)",
            },
        },
        "required": ["url"],
    },
)
async def browser_navigate(args: dict[str, Any]) -> dict[str, Any]:
    url = require_str(args["url"], "url", max_len=2048)
    wait_for = args.get("wait_for", "load")
    require_in(wait_for, "wait_for", ["load", "domcontentloaded", "networkidle"])

    # Security check
    err_msg = _check_url(url)
    if err_msg:
        return err(err_msg, code="blocked_url")

    try:
        _, _, page = await _ensure_browser()
        response = await page.goto(url, wait_until=wait_for, timeout=30000)

        if response is None:
            return err("navigation failed (no response)", code="navigation_failed")

        # Additional wait for dynamic content (JS frameworks, lazy-loaded images)
        # This ensures screenshots capture fully rendered pages
        await asyncio.sleep(2.0)

        return ok({
            "url": page.url,
            "status": response.status,
            "title": await page.title(),
            "final_url": page.url,  # After redirects
        })
    except PlaywrightTimeout:
        return err("navigation timeout (30s)", code="timeout")
    except Exception as e:
        return err(f"navigation error: {repr(e)}", code="exception")


@server.tool(
    name="browser_click",
    description="Click element by CSS selector",
    input_schema={
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector for element to click"},
            "button": {
                "type": "string",
                "enum": ["left", "right", "middle"],
                "description": "Mouse button (default: left)",
            },
        },
        "required": ["selector"],
    },
)
async def browser_click(args: dict[str, Any]) -> dict[str, Any]:
    selector = require_str(args["selector"], "selector", max_len=1024)
    button = args.get("button", "left")
    require_in(button, "button", ["left", "right", "middle"])

    # Security check
    err_msg = _sanitize_selector(selector)
    if err_msg:
        return err(err_msg, code="blocked_selector")

    try:
        _, _, page = await _ensure_browser()
        await page.click(selector, button=button, timeout=30000)
        return ok({"selector": selector, "button": button})
    except PlaywrightTimeout:
        return err(f"element not found or not clickable: {selector}", code="timeout")
    except Exception as e:
        return err(f"click error: {repr(e)}", code="exception")


@server.tool(
    name="browser_type",
    description="Type text into element by CSS selector",
    input_schema={
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector for input element"},
            "text": {"type": "string", "description": "Text to type"},
            "delay_ms": {
                "type": "integer",
                "description": "Delay between keystrokes in ms (default: 50)",
                "minimum": 0,
                "maximum": 1000,
            },
        },
        "required": ["selector", "text"],
    },
)
async def browser_type(args: dict[str, Any]) -> dict[str, Any]:
    selector = require_str(args["selector"], "selector", max_len=1024)
    text = require_str(args["text"], "text", max_len=10000)
    delay_ms = args.get("delay_ms", 50)

    # Security check
    err_msg = _sanitize_selector(selector)
    if err_msg:
        return err(err_msg, code="blocked_selector")

    try:
        _, _, page = await _ensure_browser()
        await page.type(selector, text, delay=delay_ms, timeout=30000)
        return ok({"selector": selector, "text_length": len(text)})
    except PlaywrightTimeout:
        return err(f"element not found or not typeable: {selector}", code="timeout")
    except Exception as e:
        return err(f"type error: {repr(e)}", code="exception")


@server.tool(
    name="browser_screenshot",
    description="Capture screenshot of page or element",
    input_schema={
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector for element (omit for full page)",
            },
            "full_page": {
                "type": "boolean",
                "description": "Capture full scrollable page (default: false)",
            },
        },
    },
)
async def browser_screenshot(args: dict[str, Any]) -> dict[str, Any]:
    selector = args.get("selector")
    full_page = args.get("full_page", False)

    if selector:
        selector = require_str(selector, "selector", max_len=1024)
        err_msg = _sanitize_selector(selector)
        if err_msg:
            return err(err_msg, code="blocked_selector")

    try:
        _, _, page = await _ensure_browser()

        if selector:
            element = await page.query_selector(selector)
            if element is None:
                return err(f"element not found: {selector}", code="not_found")
            screenshot_bytes = await element.screenshot(timeout=30000)
        else:
            screenshot_bytes = await page.screenshot(full_page=full_page, timeout=30000)

        b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        return ok({
            "screenshot_base64": b64,
            "size_bytes": len(screenshot_bytes),
            "selector": selector,
            "full_page": full_page,
        })
    except PlaywrightTimeout:
        return err("screenshot timeout (30s)", code="timeout")
    except Exception as e:
        return err(f"screenshot error: {repr(e)}", code="exception")


# ─── Cleanup ──────────────────────────────────────────────────────────────


async def cleanup():
    """Close browser and release resources. Called on shutdown."""
    global _browser, _context, _page
    if _page:
        await _page.close()
        _page = None
    if _context:
        await _context.close()
        _context = None
    if _browser:
        await _browser.close()
        _browser = None


def get_server() -> Server:
    """Return the computer-use MCP server instance."""
    return server
