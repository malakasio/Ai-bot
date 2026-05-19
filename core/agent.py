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


# ─── Constants ────────────────────────────────────────────────────────────

MAX_ITERATIONS: int = 20
MAX_CONSECUTIVE_FAILURES: int = 5
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
DEFAULT_MODEL: str = "claude-sonnet-latest"

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
            if k in {
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "module",
                "msecs", "message", "msg", "name", "pathname", "process",
                "processName", "relativeCreated", "stack_info", "thread",
                "threadName",
            }:
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_logger: Optional[logging.Logger] = None


def get_logger() -> logging.Logger:
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

    _logger = log
    log.info("agent logger initialized", extra={"trace_path": str(trace_path)})
    return log


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
    """
    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:
        raise RuntimeError(
            "anthropic SDK not installed; pip install anthropic"
        ) from e
    api_key = _load_api_key()
    return AsyncAnthropic(api_key=api_key)


# ─── Tenacity retry decorator ─────────────────────────────────────────────


def _is_transient(exc: BaseException) -> bool:
    """Classify an exception as transient (worth retrying)."""
    # Defer imports so absence of the SDK doesn't break import-time.
    try:
        from anthropic import (  # type: ignore[attr-defined]
            APIConnectionError, APITimeoutError, RateLimitError,
            InternalServerError, APIStatusError,
        )
        if isinstance(exc, (APIConnectionError, APITimeoutError,
                            RateLimitError, InternalServerError)):
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


async def execute_mcp_tool(name: str, tool_input: dict[str, Any]) -> Any:
    """Dispatch a tool_use block to the appropriate MCP server.

    The handler is expected to honor zone validation, snapshot rules, and
    audit logging itself (per CLAUDE.md). Errors propagate.
    """
    handler = _MCP_TOOLS.get(name)
    if handler is None:
        raise KeyError(f"no MCP handler registered for tool: {name}")
    log = get_logger()
    t0 = time.monotonic()
    try:
        result = await handler(tool_input)
        log.info("mcp.tool.ok", extra={
            "tool": name,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        })
        return result
    except Exception as e:
        log.error("mcp.tool.fail", extra={
            "tool": name,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "exc": repr(e),
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
    return await client.messages.create(**kwargs)


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
) -> AgentRun:
    """Run the agent loop.

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

        try:
            response = await _llm_call(
                client,
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_output_tokens,
            )
            run.breaker.record_success()
        except Exception as e:
            run.breaker.record_failure()
            log.error("agent.iter.fail", extra={
                "run_id": run.run_id, "iteration": run.iterations,
                "consecutive_failures": run.breaker.consecutive_failures,
                "exc": repr(e),
            })
            if run.breaker.tripped:
                run.stopped_reason = "circuit_breaker_tripped"
                await send_telegram_alert(
                    f"JARVIS: circuit breaker tripped "
                    f"after {run.breaker.consecutive_failures} consecutive "
                    f"failures on run {run.run_id}. Last error: {e!r}"
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
            btype = getattr(block, "type", None) or (
                block.get("type") if isinstance(block, dict) else None
            )
            if btype == "text":
                text = getattr(block, "text", None) or (
                    block.get("text") if isinstance(block, dict) else ""
                )
                text_chunks.append(text or "")
                assistant_blocks_for_message.append({"type": "text", "text": text or ""})
            elif btype == "tool_use":
                tool_use_blocks.append(block)
                assistant_blocks_for_message.append({
                    "type": "tool_use",
                    "id": getattr(block, "id", None) or block.get("id"),
                    "name": getattr(block, "name", None) or block.get("name"),
                    "input": getattr(block, "input", None) or block.get("input", {}),
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
            tu_id = getattr(tu, "id", None) or tu.get("id")
            tu_name = getattr(tu, "name", None) or tu.get("name")
            tu_input = getattr(tu, "input", None) or tu.get("input", {}) or {}
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
                    await send_telegram_alert(
                        f"JARVIS: circuit breaker tripped on tool failures "
                        f"({run.breaker.consecutive_failures} consecutive) "
                        f"in run {run.run_id}. Last: {tu_name} -> {e!r}"
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
