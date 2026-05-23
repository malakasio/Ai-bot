"""JARVIS core agent loop.

Responsibilities:
  * Build an async Anthropic client, sourcing the API key from a systemd
    LoadCredential file when running under systemd, falling back to the
    ANTHROPIC_API_KEY environment variable.
  * Estimate token usage before every LLM call and short-circuit when the
    budget would be exceeded.
  * Retry transient LLM errors with exponential backoff (tenacity).
  * Route tool_use blocks to MCP servers.
  * Trip a circuit breaker after N consecutive failures and notify the
    operator via Telegram.
  * Emit a structured trace to /var/log/jarvis_agent.trace (one JSON
    object per line, fsync-safe append).
  * Collect metrics via core/metrics.py for observability.

This module is import-safe even if anthropic / tenacity / httpx are not
installed yet — symbol resolution happens at call time so the package
imports cleanly on a fresh checkout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

# ─── Load .env at import time ─────────────────────────────────────────────
#
# python-dotenv is listed in requirements.txt. We import it defensively so
# the module still loads on a fresh checkout where the dep isn't installed
# yet (e.g. during CI before `pip install`). The .env lookup walks upward
# from this file so it works regardless of CWD when the agent is invoked.
try:
    from dotenv import load_dotenv as _load_dotenv  # type: ignore
    _ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_FILE.is_file():
        _load_dotenv(_ENV_FILE, override=False)
    else:
        # Fall back to dotenv's own search (CWD-upwards).
        _load_dotenv(override=False)
except Exception:
    # No dotenv installed, or unreadable .env — env vars set by the shell
    # / systemd still apply, so this is non-fatal.
    pass


# ─── Constants ────────────────────────────────────────────────────────────

MAX_ITERATIONS: int = 20
MAX_CONSECUTIVE_FAILURES: int = 10
RETRY_ATTEMPTS: int = 5
RETRY_MIN_WAIT_S: float = 1.0
RETRY_MAX_WAIT_S: float = 30.0

# Pre-flight estimation: ~4 chars per token is a reasonable English heuristic
# for the Claude family. We err on the high side (conservative ~3.5) so the
# guard rarely under-estimates.
CHARS_PER_TOKEN: float = 3.5
DEFAULT_INPUT_TOKEN_LIMIT: int = 180_000   # Claude-Sonnet/Opus context cap
DEFAULT_OUTPUT_TOKEN_BUDGET: int = 4_096

# Default model — overridable via env JARVIS_AGENT_MODEL.
DEFAULT_MODEL: str = "claude-sonnet-4-5"

TRACE_LOG_PATH = Path("/var/log/jarvis_agent.trace")
TRACE_LOG_FALLBACK = Path.home() / ".local/share/jarvis/jarvis_agent.trace"


# ─── Logging ──────────────────────────────────────────────────────────────


def _resolve_trace_path() -> Path:
    """Use /var/log if writable, else fall back under $HOME."""
    target = TRACE_LOG_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8"):
            pass
        return target
    except (PermissionError, OSError):
        fb = TRACE_LOG_FALLBACK
        fb.parent.mkdir(parents=True, exist_ok=True)
        return fb


# Reserved LogRecord attributes. We never let an `extra={}` field
# overwrite these via logger.makeRecord — Python's logging refuses, and
# the call site dies with KeyError("Attempt to overwrite 'X' in
# LogRecord"). The formatter has to know the full set so that:
#   (a) we skip them when serializing (they're already represented by
#       payload["level"], payload["logger"], etc.), and
#   (b) the helper below can rename colliding keys before they reach
#       logger.info(..., extra=...).
_RESERVED_RECORD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
})


def _sanitize_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Rename keys that collide with LogRecord attributes.

    Anything in _RESERVED_RECORD_ATTRS would make logger.makeRecord raise
    KeyError. We prefix with `extra_` so the data survives in the trace.
    """
    if not extra:
        return extra
    safe: dict[str, Any] = {}
    for k, v in extra.items():
        safe[f"extra_{k}" if k in _RESERVED_RECORD_ATTRS else k] = v
    return safe


class _JsonLineFormatter(logging.Formatter):
    """Emit one JSON object per line so the trace is grep-able and tail-able."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything attached via logger.<level>(..., extra={...}) gets merged.
        for k, v in record.__dict__.items():
            if k in _RESERVED_RECORD_ATTRS:
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _SafeExtraAdapter(logging.LoggerAdapter):
    """LoggerAdapter that sanitizes `extra={}` to avoid LogRecord collisions.

    Any caller key that shadows a reserved LogRecord attribute (`name`,
    `message`, `module`, ...) is renamed to `extra_<key>` before reaching
    logger.makeRecord. Without this, e.g. ``log.info("x", extra={"name":
    ...})`` raises ``KeyError("Attempt to overwrite 'name' in
    LogRecord")`` and crashes the caller.
    """

    def process(self, msg, kwargs):  # type: ignore[override]
        extra = kwargs.get("extra")
        if extra:
            kwargs["extra"] = _sanitize_extra(extra)
        return msg, kwargs


_logger: Optional[logging.LoggerAdapter] = None


def get_logger() -> logging.LoggerAdapter:
    """Lazy logger init so importing the module never fails on FS issues."""
    global _logger
    if _logger is not None:
        return _logger

    log = logging.getLogger("jarvis.agent")
    log.setLevel(logging.INFO)
    log.propagate = False

    trace_path = _resolve_trace_path()
    handler = logging.handlers.RotatingFileHandler(
        trace_path,
        maxBytes=int(os.environ.get("JARVIS_TRACE_ROTATE_BYTES", 50 * 1024 * 1024)),
        backupCount=int(os.environ.get("JARVIS_TRACE_KEEP", 5)),
        encoding="utf-8",
    )
    handler.setFormatter(_JsonLineFormatter())
    log.addHandler(handler)

    if os.environ.get("JARVIS_AGENT_STDERR_TRACE") == "1":
        sh = logging.StreamHandler()
        sh.setFormatter(_JsonLineFormatter())
        log.addHandler(sh)

    adapter = _SafeExtraAdapter(log, {})
    _logger = adapter
    adapter.info("agent logger initialized",
                 extra={"trace_path": str(trace_path)})
    return adapter


# ─── Credentials ──────────────────────────────────────────────────────────


def _load_api_key() -> str:
    """Resolve the Anthropic API key.

    1. systemd LoadCredential — when the service unit declares
       ``LoadCredential=anthropic_api_key:/etc/jarvis/anthropic_api_key``,
       systemd places the file under ``$CREDENTIALS_DIRECTORY`` and the
       process sees that env var.
    2. JARVIS_ANTHROPIC_CRED_NAME env var lets the unit pick a different
       credential name.
    3. Fallback: ANTHROPIC_API_KEY env var.

    Raises RuntimeError if nothing resolves — the agent refuses to start.
    """
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    cred_name = os.environ.get("JARVIS_ANTHROPIC_CRED_NAME", "anthropic_api_key")
    if cred_dir:
        cred_path = Path(cred_dir) / cred_name
        if cred_path.is_file():
            key = cred_path.read_text(encoding="utf-8").strip()
            if key:
                return key

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key

    raise RuntimeError(
        "no Anthropic API key found: set ANTHROPIC_API_KEY or run under "
        "systemd with LoadCredential=anthropic_api_key:..."
    )


# ─── Async client ─────────────────────────────────────────────────────────


def build_async_client() -> Any:
    """Construct an AsyncAnthropic client. Import is deferred so the module
    loads even when the SDK isn't installed yet (e.g. CI before pip install).

    Honors ANTHROPIC_BASE_URL — when set, the client is pointed at the
    given gateway (e.g. an Anthropic-compatible proxy) instead of the
    default https://api.anthropic.com. The SDK reads this env var itself,
    but we pass it explicitly so the behavior is obvious from the trace.

    **Proxy authentication:** When ANTHROPIC_BASE_URL is set, the client
    sends `Authorization: Bearer <key>` instead of the SDK's default
    `x-api-key: <key>` header. This matches the OAuth2 Bearer token format
    expected by most API gateways (e.g. neutralbeats). Direct Anthropic API
    calls (no base_url) continue using the standard x-api-key header.

    Timeout defaults to 300s (5 minutes) to prevent indefinite hangs when
    proxies return empty responses. Override via JARVIS_API_TIMEOUT.
    """
    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:
        raise RuntimeError(
            "anthropic SDK not installed; pip install anthropic"
        ) from e
    api_key = _load_api_key()
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    timeout = float(os.environ.get("JARVIS_API_TIMEOUT", "300.0"))

    if base_url:
        # Proxy mode: use Authorization: Bearer header instead of x-api-key.
        # The SDK appends /v1 to base_url, so strip it if present to avoid /v1/v1.
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]

        # Use Omit() to explicitly tell the SDK not to add X-Api-Key header.
        # This prevents header conflicts when the proxy expects only Bearer auth.
        try:
            from anthropic._types import NOT_GIVEN, Omit
            import httpx

            http_client = httpx.AsyncClient(timeout=timeout)
            kwargs: dict[str, Any] = {
                "api_key": NOT_GIVEN,
                "base_url": base_url,
                "http_client": http_client,
                "default_headers": {
                    "Authorization": f"Bearer {api_key}",
                    "X-Api-Key": Omit()
                }
            }
        except ImportError:
            # Fallback if SDK version doesn't support Omit
            import httpx
            http_client = httpx.AsyncClient(
                timeout=timeout,
                headers={"Authorization": f"Bearer {api_key}"}
            )
            kwargs: dict[str, Any] = {
                "api_key": "unused",
                "base_url": base_url,
                "http_client": http_client
            }

        try:
            get_logger().info("anthropic.client.proxy_mode",
                              extra={
                                  "base_url": base_url,
                                  "timeout": timeout,
                                  "auth_header": "Bearer"
                              })
        except Exception:
            pass
    else:
        # Direct mode: use standard x-api-key authentication
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}

    return AsyncAnthropic(**kwargs)


# ─── Tenacity retry decorator ─────────────────────────────────────────────


def _is_transient(exc: BaseException) -> bool:
    """Classify an exception as transient (worth retrying)."""
    # json.JSONDecodeError means the upstream returned an empty / invalid
    # body — e.g. a proxy (giftcat) returning HTTP 200 with no content.
    # This is inherently transient; the next attempt may get a real body.
    if isinstance(exc, json.JSONDecodeError):
        return True

    # Defer imports so absence of the SDK doesn't break import-time.
    try:
        from anthropic import (  # type: ignore[attr-defined]
            APIConnectionError, APITimeoutError, RateLimitError,
            InternalServerError, APIStatusError,
        )
        if isinstance(exc, (APIConnectionError,
                            APITimeoutError,
                            RateLimitError,
                            InternalServerError)):
            return True
        if isinstance(exc, APIStatusError):
            return 500 <= getattr(exc, "status_code", 0) < 600
    except Exception:
        pass

    try:
        import httpx
        if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout,
                            httpx.RemoteProtocolError, httpx.WriteTimeout)):
            return True
    except Exception:
        pass

    # Generic timeouts always retry.
    return isinstance(exc, (asyncio.TimeoutError, ConnectionError, TimeoutError))


def retry_transient(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Decorate an async callable with exponential-backoff retry.

    Uses tenacity if available; otherwise a hand-rolled fallback with the
    same semantics (5 attempts, exp backoff between 1 s and 30 s, multiplier
    2, plus a small jitter).
    """
    try:
        from tenacity import (
            AsyncRetrying, stop_after_attempt, wait_random_exponential,
            retry_if_exception, before_sleep_log,
        )

        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            log = get_logger()
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(RETRY_ATTEMPTS),
                wait=wait_random_exponential(
                    multiplier=1, min=RETRY_MIN_WAIT_S, max=RETRY_MAX_WAIT_S,
                ),
                retry=retry_if_exception(_is_transient),
                before_sleep=before_sleep_log(log, logging.WARNING),
                reraise=True,
            ):
                with attempt:
                    return await fn(*args, **kwargs)
        return wrapped

    except ImportError:
        # Fallback that matches tenacity's wait_random_exponential semantics
        # closely enough for production: exp backoff, capped, jittered.
        import random

        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            log = get_logger()
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                try:
                    return await fn(*args, **kwargs)
                except Exception as e:
                    if attempt >= RETRY_ATTEMPTS or not _is_transient(e):
                        raise
                    delay = min(
                        RETRY_MAX_WAIT_S,
                        RETRY_MIN_WAIT_S * (2 ** (attempt - 1)),
                    )
                    delay *= 0.5 + random.random() * 0.5  # jitter
                    log.warning(
                        "transient LLM error, retrying",
                        extra={
                            "attempt": attempt,
                            "max_attempts": RETRY_ATTEMPTS,
                            "delay_s": round(delay, 2),
                            "exc": repr(e),
                        },
                    )
                    await asyncio.sleep(delay)
        return wrapped


# ─── Token estimation ─────────────────────────────────────────────────────


def estimate_tokens(messages: list[dict[str, Any]],
                    system: Optional[str] = None,
                    tools: Optional[list[dict[str, Any]]] = None) -> int:
    """Conservative pre-flight estimate of input tokens for a Messages call.

    Walks the structured request the same way the API does and sums character
    counts, then divides by CHARS_PER_TOKEN. Adds a small per-message
    overhead so we don't under-count framing tokens.
    """
    chars = 0
    if system:
        chars += len(system)
    if tools:
        chars += len(json.dumps(tools, ensure_ascii=False))
    for m in messages:
        chars += 8  # framing tokens per message
        content = m.get("content", "")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    chars += len(str(block))
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    chars += len(block.get("text", ""))
                elif btype == "tool_use":
                    chars += len(json.dumps(block.get("input", {}),
                                            ensure_ascii=False))
                    chars += len(block.get("name", "")) + 16
                elif btype == "tool_result":
                    tc = block.get("content", "")
                    if isinstance(tc, str):
                        chars += len(tc)
                    else:
                        chars += len(json.dumps(tc, ensure_ascii=False))
                else:
                    chars += len(json.dumps(block, ensure_ascii=False))
    return int(chars / CHARS_PER_TOKEN) + 8


class TokenBudgetExceeded(RuntimeError):
    """Raised by run_jarvis_core() when an LLM call would blow the budget."""


# ─── Circuit breaker ──────────────────────────────────────────────────────


@dataclass
class CircuitBreaker:
    threshold: int = MAX_CONSECUTIVE_FAILURES
    consecutive_failures: int = 0
    tripped: bool = False

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.tripped = True

    def reset(self) -> None:
        self.consecutive_failures = 0
        self.tripped = False


async def send_telegram_alert(text: str) -> bool:
    """Best-effort Telegram notification. Returns True on success."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_USER_ID", "").strip() or \
              os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        get_logger().warning("telegram alert skipped: missing creds")
        return False
    try:
        import httpx
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as c:
            resp = await c.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            })
            ok = resp.status_code == 200 and resp.json().get("ok", False)
            get_logger().info("telegram alert sent",
                              extra={"ok": ok, "status": resp.status_code})
            return ok
    except Exception as e:
        get_logger().error("telegram alert failed", extra={"exc": repr(e)})
        return False


# ─── MCP tool router ──────────────────────────────────────────────────────


# Registry mapped tool_name -> async callable(input_dict) -> result.
# Filled in by the orchestrator at startup; kept module-level so tests can
# register stubs.
_MCP_TOOLS: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {}


def register_mcp_tool(
    name: str,
    handler: Callable[[dict[str, Any]], Awaitable[Any]],
) -> None:
    _MCP_TOOLS[name] = handler


def clear_mcp_tools() -> None:
    _MCP_TOOLS.clear()


# ─── Destructive-op snapshot guard (P3) ───────────────────────────────────
#
# Any tool whose qualified name (e.g. "filesystem.write_file") or bare name
# is in DESTRUCTIVE_TOOLS triggers a pre-mutation snapshot before execution.
# The snapshot tag is attached to the tool result so the agent — and the
# audit log — know which checkpoint to roll back to.
#
# Callers can extend the set at startup:
#     from core import agent
#     agent.DESTRUCTIVE_TOOLS.add("custom.dangerous_op")

DESTRUCTIVE_TOOLS: set[str] = {
    # filesystem MCP
    "write_file", "delete",
    "filesystem.write_file", "filesystem.delete",
    # shell MCP, if registered
    "exec", "shell.exec",
    # git MCP — only mutating ops
    "commit", "git.commit", "stage", "git.stage",
    # P3 protocol scope: local filesystem mutation only.
    # External side-effects (n8n, apify, browser) do NOT require snapshots.
}


def is_destructive(tool_name: str) -> bool:
    """Return True if the tool needs a pre-mutation snapshot.

    Handles three name shapes:
      * ``"write_file"``               — bare
      * ``"filesystem.write_file"``    — dotted (legacy MCP qualified)
      * ``"filesystem__write_file"``   — sanitized for the Anthropic API
        (dots are illegal in tool names per ``^[a-zA-Z0-9_-]{1,128}$``,
        so the Telegram bridge rewrites ``.`` → ``__``)
    """
    if tool_name in DESTRUCTIVE_TOOLS:
        return True
    # Match bare name when caller passed qualified, and vice versa.
    bare_dot = tool_name.split(".", 1)[-1]
    if bare_dot in DESTRUCTIVE_TOOLS:
        return True
    bare_us = tool_name.split("__", 1)[-1]
    if bare_us in DESTRUCTIVE_TOOLS:
        return True
    # Also reconstruct the dotted qualified form from a sanitized name
    # and check the set directly (e.g. "filesystem__write_file" →
    # "filesystem.write_file").
    if "__" in tool_name:
        rebuilt = tool_name.replace("__", ".")
        if rebuilt in DESTRUCTIVE_TOOLS:
            return True
    return False


async def _create_snapshot(reason: str) -> Optional[str]:
    """Shell out to scripts/snapshot.sh. Returns the tag, or None on failure.

    Best-effort by design: a snapshot failure should NOT abort the tool
    call itself when JARVIS_REQUIRE_SNAPSHOT is false. When the env flag
    is true the caller should treat None as a hard stop.
    """
    log = get_logger()
    script = Path(__file__).resolve().parent.parent / "scripts" / "snapshot.sh"
    if not script.exists():
        log.warning("agent.snapshot.script_missing",
                    extra={"path": str(script)})
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(script), reason, "--no-push",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                timeout=30.0)
    except Exception as e:
        log.warning("agent.snapshot.exec_failed", extra={"exc": repr(e)})
        return None
    if proc.returncode != 0:
        log.warning("agent.snapshot.rc_nonzero", extra={
            "rc": proc.returncode,
            "stderr": stderr.decode("utf-8", "replace")[:512],
        })
        return None
    tag = stdout.decode("utf-8", "replace").strip().splitlines()[-1:]
    return tag[0] if tag else None

def _attr_or_dict(obj: Any, field: str, default: Any = None) -> Any:
    """Safely read *field* from an SDK object (via getattr) or a dict (via .get).

    Avoids the ``getattr(obj, f, None) or obj.get(f, d)`` pattern, which
    crashes on ToolUseBlock when *field* exists but is a falsy value such
    as an empty dict ``{}`` or an empty string ``""``.
    """
    if isinstance(obj, dict):
        return obj.get(field, default)
    attr = getattr(obj, field, None)
    return default if attr is None else attr



async def execute_mcp_tool(name: str, tool_input: dict[str, Any]) -> Any:
    """Dispatch a tool_use block to the appropriate MCP server.

    The handler is expected to honor zone validation, snapshot rules, and
    audit logging itself (per CLAUDE.md). Errors propagate.

    Before any tool in DESTRUCTIVE_TOOLS runs, a pre-mutation snapshot
    is taken via scripts/snapshot.sh. If JARVIS_REQUIRE_SNAPSHOT=true
    (the default in hardcore mode) and the snapshot fails, the tool
    call is refused. Otherwise the call proceeds and the snapshot
    failure is logged.
    """
    handler = _MCP_TOOLS.get(name)
    if handler is None:
        raise KeyError(f"no MCP handler registered for tool: {name}")

    log = get_logger()

    snapshot_tag: Optional[str] = None
    if is_destructive(name):
        require = os.environ.get("JARVIS_REQUIRE_SNAPSHOT", "true") \
                    .lower() in {"1", "true", "yes", "on"}
        reason = f"agent:tool:{name}"
        snapshot_tag = await _create_snapshot(reason)
        if snapshot_tag:
            log.info("agent.snapshot.created",
                     extra={"tool": name, "tag": snapshot_tag})
        else:
            log.warning("agent.snapshot.unavailable",
                        extra={"tool": name, "require": require})
            if require:
                raise RuntimeError(
                    f"refusing destructive tool {name!r}: snapshot failed "
                    "and JARVIS_REQUIRE_SNAPSHOT=true"
                )

    # Instrument with the observability layer (OTel span + trace events).
    try:
        from observability import tracing as _otrace
        _tool_ctx = _otrace.instrument_tool_call(
            tool=name,
            is_destructive=is_destructive(name),
            snapshot_tag=snapshot_tag,
        )
    except Exception:
        # Observability missing? Fall back to a no-op context.
        import contextlib as _ctx
        @_ctx.contextmanager
        def _noop_tool():
            yield {}
        _tool_ctx = _noop_tool()

    t0 = time.monotonic()
    with _tool_ctx as _:
        try:
            result = await handler(tool_input)
            log.info("mcp.tool.ok", extra={
                "tool": name,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "snapshot": snapshot_tag,
            })
            # Decorate the result with the rollback tag when applicable.
            if snapshot_tag and isinstance(result, dict):
                result = {**result, "_snapshot": snapshot_tag}
            return result
        except Exception as e:
            log.error("mcp.tool.fail", extra={
                "tool": name,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "exc": repr(e),
                "snapshot": snapshot_tag,
            })
            raise


# ─── LLM call ─────────────────────────────────────────────────────────────


@retry_transient
async def _llm_call(
    client: Any,
    *,
    model: str,
    system: Optional[str],
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]],
    max_tokens: int,
) -> Any:
    """Single retried Messages.create call."""
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools
    base_url = os.environ.get('ANTHROPIC_BASE_URL', '').strip()
    try:
        return await client.messages.create(**kwargs)
    except json.JSONDecodeError as e:
        ctx = f'anthropic_api={base_url}' if base_url else 'direct_anthropic'
        get_logger().warning(
            f'LLM returned empty / invalid JSON response '
            f'({ctx}) — will retry'
        )
        raise


# ─── Orchestrator integration ────────────────────────────────────────────


def _should_use_orchestrator(prompt: str) -> bool:
    """Heuristic detection of complex tasks that benefit from orchestration.

    Checks:
    - Prompt length (> threshold)
    - Complexity keywords (analyze, research, investigate, etc.)
    - Multiple questions (3+)

    Returns:
        True if task appears complex enough for multi-agent orchestration.
    """
    log = get_logger()

    # Check if orchestrator is enabled
    enable_orch_raw = os.environ.get("JARVIS_ENABLE_ORCHESTRATOR", "false")
    enable_orch = enable_orch_raw.lower() == "true"

    log.info("orchestrator.should_use.check", extra={
        "JARVIS_ENABLE_ORCHESTRATOR_raw": enable_orch_raw,
        "JARVIS_ENABLE_ORCHESTRATOR_parsed": enable_orch,
        "prompt_length": len(prompt),
    })

    # Length threshold (configurable via env)
    min_length = int(os.environ.get("JARVIS_ORCHESTRATOR_MIN_LENGTH", "800"))
    length_check = len(prompt) > min_length

    log.info("orchestrator.should_use.length_check", extra={
        "prompt_length": len(prompt),
        "min_length_threshold": min_length,
        "length_check_passed": length_check,
    })

    if length_check:
        log.info("orchestrator.should_use.decision", extra={
            "decision": True,
            "reason": "prompt_length_exceeded",
            "prompt_length": len(prompt),
            "threshold": min_length,
        })
        return True

    # Keyword detection
    complexity_keywords = [
        "analyze", "compare", "research", "investigate",
        "comprehensive", "thorough", "deep dive", "ultrathink",
        "multiple", "various", "several aspects", "explore",
        "examine", "study", "review", "assess", "evaluate"
    ]
    prompt_lower = prompt.lower()
    detected_keywords = [kw for kw in complexity_keywords if kw in prompt_lower]
    keyword_count = len(detected_keywords)
    keyword_check = keyword_count >= 3

    log.info("orchestrator.should_use.keyword_check", extra={
        "detected_keywords": detected_keywords,
        "keyword_count": keyword_count,
        "keyword_threshold": 3,
        "keyword_check_passed": keyword_check,
    })

    if keyword_check:
        log.info("orchestrator.should_use.decision", extra={
            "decision": True,
            "reason": "keyword_threshold_exceeded",
            "detected_keywords": detected_keywords,
            "keyword_count": keyword_count,
        })
        return True

    # Multiple questions heuristic
    question_count = prompt.count("?")
    question_check = question_count >= 3

    log.info("orchestrator.should_use.question_check", extra={
        "question_count": question_count,
        "question_threshold": 3,
        "question_check_passed": question_check,
    })

    if question_check:
        log.info("orchestrator.should_use.decision", extra={
            "decision": True,
            "reason": "multiple_questions",
            "question_count": question_count,
        })
        return True

    log.info("orchestrator.should_use.decision", extra={
        "decision": False,
        "reason": "no_complexity_indicators",
        "prompt_length": len(prompt),
        "keyword_count": keyword_count,
        "question_count": question_count,
    })
    return False


# ─── Main loop ────────────────────────────────────────────────────────────


@dataclass
class AgentRun:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    iterations: int = 0
    stopped_reason: str = ""
    final_message: Optional[str] = None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)


async def run_jarvis_core(
    user_prompt: str,
    *,
    client: Optional[Any] = None,
    model: Optional[str] = None,
    system: Optional[str] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    max_iterations: int = MAX_ITERATIONS,
    max_output_tokens: int = DEFAULT_OUTPUT_TOKEN_BUDGET,
    input_token_limit: int = DEFAULT_INPUT_TOKEN_LIMIT,
    use_orchestrator: Optional[bool] = None,
) -> AgentRun:
    """Run the agent loop.

    Args:
        user_prompt: The user's input text.
        client: Anthropic client (auto-created if None).
        model: Model name (defaults to JARVIS_AGENT_MODEL or DEFAULT_MODEL).
        system: System prompt override.
        tools: Tool definitions for tool use.
        max_iterations: Maximum agent loop iterations.
        max_output_tokens: Token budget per LLM call.
        input_token_limit: Pre-flight token budget check.
        use_orchestrator: If True, use multi-agent orchestration for complex tasks.
                         If None (default), auto-detect based on task complexity
                         and JARVIS_ENABLE_ORCHESTRATOR env var.
                         If False, always use single-agent loop.

    Returns:
        AgentRun with final_message, transcript, and metadata.

    Stops when:
      * the model returns a turn with stop_reason != 'tool_use' (final answer),
      * `max_iterations` is reached,
      * the circuit breaker trips (5 consecutive failures),
      * pre-flight token estimation would exceed `input_token_limit`.
    """
    log = get_logger()
    run = AgentRun()
    log.info("agent.run.start", extra={
        "run_id": run.run_id, "model": model or DEFAULT_MODEL,
        "max_iterations": max_iterations,
    })

    # ─── Orchestration routing ───────────────────────────────────────────
    # Check if task should use multi-agent orchestration.
    # Feature is opt-in via JARVIS_ENABLE_ORCHESTRATOR env var.

    log.info("agent.orchestration.routing_start", extra={
        "run_id": run.run_id,
        "use_orchestrator_param": use_orchestrator,
        "prompt_length": len(user_prompt),
    })

    if use_orchestrator is None:
        # Auto-detect complexity if orchestration is enabled
        enable_orch_raw = os.environ.get("JARVIS_ENABLE_ORCHESTRATOR", "false")
        enable_orch = enable_orch_raw.lower() == "true"

        log.info("agent.orchestration.env_check", extra={
            "run_id": run.run_id,
            "JARVIS_ENABLE_ORCHESTRATOR_raw": enable_orch_raw,
            "JARVIS_ENABLE_ORCHESTRATOR_parsed": enable_orch,
        })

        if enable_orch:
            log.info("agent.orchestration.calling_should_use", extra={
                "run_id": run.run_id,
                "prompt_length": len(user_prompt),
            })
            use_orchestrator = _should_use_orchestrator(user_prompt)
            log.info("agent.orchestration.auto_detect", extra={
                "run_id": run.run_id,
                "use_orchestrator": use_orchestrator,
                "prompt_length": len(user_prompt),
            })
        else:
            log.info("agent.orchestration.disabled", extra={
                "run_id": run.run_id,
                "reason": "JARVIS_ENABLE_ORCHESTRATOR not true",
            })
            use_orchestrator = False
    else:
        log.info("agent.orchestration.explicit_param", extra={
            "run_id": run.run_id,
            "use_orchestrator": use_orchestrator,
        })

    if use_orchestrator:
        log.info("agent.orchestration.start", extra={"run_id": run.run_id})
        try:
            # Lazy import to avoid circular dependency
            # (orchestrator.py imports run_jarvis_core)
            from core.orchestrator import run_orchestrated

            # Run multi-agent orchestration
            orch_result = await run_orchestrated(user_prompt)

            # Convert orchestrator dict to AgentRun format
            run.stopped_reason = "orchestration_complete"
            run.final_message = orch_result.get("output", "")
            run.iterations = orch_result.get("subtasks", 0)

            # Add orchestration metadata to transcript
            run.transcript.append({
                "type": "orchestration_summary",
                "success": orch_result.get("success", False),
                "score": orch_result.get("score", 0.0),
                "duration_ms": orch_result.get("duration_ms", 0.0),
                "subtasks": orch_result.get("subtasks", 0),
                "accepted": orch_result.get("accepted", 0),
            })

            log.info("agent.orchestration.complete", extra={
                "run_id": run.run_id,
                "score": orch_result.get("score"),
                "subtasks": orch_result.get("subtasks"),
                "accepted": orch_result.get("accepted"),
                "duration_ms": orch_result.get("duration_ms"),
            })

            return run

        except Exception as e:
            log.error("agent.orchestration.failed", extra={
                "run_id": run.run_id,
                "exc": repr(e),
            })
            # Fall back to single-agent loop
            log.info("agent.orchestration.fallback_to_single", extra={
                "run_id": run.run_id,
            })
            use_orchestrator = False

    # ─── Single-agent loop (existing behavior) ───────────────────────────

    if client is None:
        client = build_async_client()
    model = model or os.environ.get("JARVIS_AGENT_MODEL", DEFAULT_MODEL)

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": user_prompt},
    ]

    while run.iterations < max_iterations:
        run.iterations += 1

        # Pre-flight token check.
        est = estimate_tokens(messages, system=system, tools=tools)
        if est > input_token_limit:
            run.stopped_reason = "token_budget_exceeded"
            log.error("agent.run.budget_exceeded", extra={
                "run_id": run.run_id, "estimated_tokens": est,
                "limit": input_token_limit, "iteration": run.iterations,
            })
            await send_telegram_alert(
                f"JARVIS: token budget exceeded "
                f"(~{est} > {input_token_limit}) on run {run.run_id}"
            )
            break

        log.info("agent.iter.start", extra={
            "run_id": run.run_id, "iteration": run.iterations,
            "estimated_input_tokens": est,
        })

        # Observability: open an LLM span+event around the retried call.
        try:
            from observability import tracing as _otrace
            _llm_ctx = _otrace.instrument_llm_call(
                model=model,
                run_id=run.run_id,
                iteration=run.iterations,
                estimated_input_tokens=est,
            )
        except Exception:
            import contextlib as _ctx
            @_ctx.contextmanager
            def _noop_llm():
                yield {}
            _llm_ctx = _noop_llm()

        try:
            with _llm_ctx as _llm_record:
                response = await _llm_call(
                    client,
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_output_tokens,
                )
                # Best-effort token usage capture for the trace.
                try:
                    usage = getattr(response, "usage", None)
                    if usage is not None:
                        _llm_record["input_tokens"] = getattr(
                            usage, "input_tokens", None)
                        _llm_record["output_tokens"] = getattr(
                            usage, "output_tokens", None)
                        # Collect metrics
                        try:
                            from core.metrics import get_metrics
                            metrics = get_metrics()
                            metrics.llm_requests_total.inc()
                            input_tok = getattr(usage, "input_tokens", 0)
                            output_tok = getattr(usage, "output_tokens", 0)
                            metrics.record_llm_cost(model, input_tok, output_tok)
                        except Exception:
                            pass
                except Exception:
                    pass
            run.breaker.record_success()
        except Exception as e:
            run.breaker.record_failure()
            log.error("agent.iter.fail", extra={
                "run_id": run.run_id, "iteration": run.iterations,
                "consecutive_failures": run.breaker.consecutive_failures,
                "exc": repr(e),
            })
            # Collect error metrics
            try:
                from core.metrics import get_metrics
                metrics = get_metrics()
                metrics.llm_errors_total.inc()
                metrics.record_error()
            except Exception:
                pass
            if run.breaker.tripped:
                # Record circuit breaker trip
                try:
                    from core.metrics import get_metrics
                    get_metrics().circuit_breaker_trips.inc()
                except Exception:
                    pass
                run.stopped_reason = "circuit_breaker_tripped"
                timeout = os.environ.get("JARVIS_API_TIMEOUT", "300.0")
                await send_telegram_alert(
                    f"JARVIS: circuit breaker tripped "
                    f"after {run.breaker.consecutive_failures} consecutive "
                    f"failures on run {run.run_id} (timeout={timeout}s). "
                    f"Last error: {e!r}"
                )
                break
            continue  # let the retry-wrapped next iteration try again

        # Extract the assistant turn into the rolling message list.
        assistant_content = getattr(response, "content", [])
        # SDK returns objects; normalize to dicts so downstream JSON works.
        assistant_blocks_for_message: list[dict[str, Any]] = []
        tool_use_blocks: list[Any] = []
        text_chunks: list[str] = []
        for block in assistant_content:
            btype = _attr_or_dict(block, "type", None)
            if btype == "text":
                text = _attr_or_dict(block, "text", "")
                text_chunks.append(text or "")
                assistant_blocks_for_message.append({"type": "text", "text": text or ""})
            elif btype == "tool_use":
                tool_use_blocks.append(block)
                assistant_blocks_for_message.append({
                    "type": "tool_use",
                    "id": _attr_or_dict(block, "id"),
                    "name": _attr_or_dict(block, "name"),
                    "input": _attr_or_dict(block, "input", {}),
                })
            else:
                assistant_blocks_for_message.append(
                    block if isinstance(block, dict) else {"type": str(btype)}
                )

        messages.append({"role": "assistant", "content": assistant_blocks_for_message})
        run.transcript.append({"role": "assistant",
                               "content": assistant_blocks_for_message})

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason != "tool_use" or not tool_use_blocks:
            run.stopped_reason = f"stop_reason={stop_reason}"
            run.final_message = "".join(text_chunks).strip() or None
            log.info("agent.run.done", extra={
                "run_id": run.run_id, "iterations": run.iterations,
                "stop_reason": stop_reason,
            })
            break

        # Run all requested tools, append results.
        tool_results: list[dict[str, Any]] = []
        for tu in tool_use_blocks:
            tu_id = _attr_or_dict(tu, "id")
            tu_name = _attr_or_dict(tu, "name")
            tu_input = _attr_or_dict(tu, "input", {})
            try:
                result = await execute_mcp_tool(tu_name, tu_input)
                run.breaker.record_success()
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": result if isinstance(result, str)
                               else json.dumps(result, ensure_ascii=False),
                })
            except Exception as e:
                run.breaker.record_failure()
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "is_error": True,
                    "content": f"error: {e!r}",
                })
                if run.breaker.tripped:
                    run.stopped_reason = "circuit_breaker_tripped"
                    timeout = os.environ.get("JARVIS_API_TIMEOUT", "300.0")
                    await send_telegram_alert(
                        f"JARVIS: circuit breaker tripped on tool failures "
                        f"({run.breaker.consecutive_failures} consecutive) "
                        f"in run {run.run_id} (timeout={timeout}s). "
                        f"Last: {tu_name} -> {e!r}"
                    )
                    break
            if run.breaker.tripped:
                break

        messages.append({"role": "user", "content": tool_results})
        run.transcript.append({"role": "user", "content": tool_results})

        if run.breaker.tripped:
            break

    if not run.stopped_reason:
        run.stopped_reason = "max_iterations"
        log.warning("agent.run.max_iterations", extra={
            "run_id": run.run_id, "iterations": run.iterations,
        })

    return run


# ─── Module-level convenience ─────────────────────────────────────────────


def main() -> None:  # pragma: no cover - CLI shim
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m core.agent '<prompt>'", file=sys.stderr)
        sys.exit(2)
    prompt = sys.argv[1]
    run = asyncio.run(run_jarvis_core(prompt))
    print(json.dumps({
        "run_id": run.run_id,
        "iterations": run.iterations,
        "stopped_reason": run.stopped_reason,
        "final_message": run.final_message,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
