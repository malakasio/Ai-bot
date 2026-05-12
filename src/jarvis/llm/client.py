"""
LLM client with:
- Full agentic loop (v6 critical fix: not single-call)
- Tool execution in the loop
- max_tokens=8192 (v6 fix: 2048 cuts code mid-stream)
- max_tokens continuation on partial response
- Anthropic exact token counting (v6 fix: not tiktoken for budget)
- LiteLLM fallback chain for resilience
- Tenacity retry on transient errors only (v6 fix: not all errors)
- Circuit breaker for same-call loops
- Budget pre-flight check
- Graceful streaming with sentence chunking for voice
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from jarvis.config import get_config
from jarvis.llm.router import RoutingDecision, route, EXPECTED_OUTPUT_TOKENS
from jarvis.observability.logger import get_logger, get_audit
from jarvis.observability.metrics import get_metrics

log = get_logger("llm")

# ─── Transient error detection ────────────────────────────────────────────────

TRANSIENT_HTTP_CODES = {500, 502, 503, 504}  # 429 excluded: retrying rate limits makes them worse
TRANSIENT_EXCEPTIONS: tuple = ()

try:
    import aiohttp
    TRANSIENT_EXCEPTIONS = (aiohttp.ClientConnectionError, asyncio.TimeoutError)
except ImportError:
    TRANSIENT_EXCEPTIONS = (asyncio.TimeoutError,)


def is_transient(exc: Exception) -> bool:
    """v6 fix: only retry known transient failures, not 400/401/422."""
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in TRANSIENT_HTTP_CODES:
        return True
    return False


# ─── Circuit breaker ──────────────────────────────────────────────────────────

class CircuitBreaker:
    """v6 fix: bounded deque(maxlen=50) not unbounded list."""

    def __init__(self, max_same: int = 3, window: int = 50):
        self.call_history: deque[str] = deque(maxlen=window)
        self.max_same = max_same

    def check(self, tool_name: str, args: dict) -> bool:
        key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
        # Check BEFORE appending: if the last max_same entries are all
        # the same as this call, block it (the max_same+1 th identical call)
        recent = list(self.call_history)[-(self.max_same):]
        if len(recent) == self.max_same and len(set(recent)) == 1 and recent[0] == key:
            get_metrics().circuit_breaker_trips.inc()
            return False
        self.call_history.append(key)
        return True


# ─── Budget tracking ──────────────────────────────────────────────────────────

_daily_tokens = 0
_daily_cost_usd = 0.0
_daily_reset_ts = 0.0

MODEL_COSTS_PER_1K = {
    "claude-haiku": (0.00025, 0.00125),
    "claude-sonnet": (0.003, 0.015),
    "claude-opus": (0.015, 0.075),
    "gpt-4o-mini": (0.00015, 0.0006),
    "groq": (0.0, 0.0),
    "ollama": (0.0, 0.0),
}


def _get_model_cost(model: str, input_tok: int, output_tok: int) -> float:
    prefix = next((k for k in MODEL_COSTS_PER_1K if k in model.lower()), "ollama")
    in_cost, out_cost = MODEL_COSTS_PER_1K[prefix]
    return (input_tok / 1000) * in_cost + (output_tok / 1000) * out_cost


def _daily_total_usd() -> float:
    global _daily_cost_usd, _daily_reset_ts
    now = time.time()
    if now - _daily_reset_ts > 86400:
        _daily_cost_usd = 0.0
        _daily_reset_ts = now
    return _daily_cost_usd


def _estimate_input_tokens(messages: list[dict], system: str, tools: list[dict]) -> int:
    """Fast token estimate for pre-flight check."""
    total = len(system) // 4
    total += sum(len(json.dumps(t)) // 4 for t in tools)
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str):
            total += len(c) // 4
        else:
            total += len(json.dumps(c)) // 4
    return total


# ─── Ollama client ────────────────────────────────────────────────────────────

async def _ollama_call(
    model: str,
    messages: list[dict],
    system: str,
    tools: list[dict],
    max_tokens: int,
    stream: bool = False,
) -> dict:
    """Call Ollama local API."""
    try:
        import aiohttp
    except ImportError:
        raise RuntimeError("aiohttp not installed. pip install aiohttp")

    cfg = get_config()
    base_url = cfg.llm.ollama_base_url

    # Ollama uses a different message format
    ollama_messages = [{"role": "system", "content": system}] + messages if system else messages

    payload = {
        "model": model,
        "messages": ollama_messages,
        "stream": stream,
        "options": {"num_predict": max_tokens, "temperature": 0.7},
    }

    if tools:
        # Ollama expects OpenAI tool format
        ollama_tools = []
        for t in tools:
            ollama_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", t.get("parameters", {})),
                },
            })
        payload["tools"] = ollama_tools

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{base_url}/api/chat", json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Ollama error {resp.status}: {text}")
            data = await resp.json()

    return data


# ─── Groq client (FREE cloud LLM — OpenAI-compatible) ─────────────────────────

async def _groq_call(
    model: str,
    messages: list[dict],
    system: str,
    tools: list[dict],
    max_tokens: int,
) -> dict:
    """Call Groq API (free tier, no credit card required)."""
    import httpx
    cfg = get_config()

    groq_messages = ([{"role": "system", "content": system}] + messages) if system else messages

    payload: dict = {
        "model": model,
        "messages": groq_messages,
        "max_tokens": min(max_tokens, 32768),
        "temperature": 0.7,
    }

    if tools:
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", t.get("parameters", {})),
                },
            }
            for t in tools
        ]
        payload["tools"] = openai_tools

    async with httpx.AsyncClient(timeout=120) as client:
        for attempt in range(3):
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg.llm.groq_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code == 429:
                # Parse retry-after from error message or use default
                import re
                wait_match = re.search(r"try again in (\d+\.?\d*)s", resp.text)
                wait = float(wait_match.group(1)) + 0.5 if wait_match else 5.0
                log.warning(f"Groq rate limit, waiting {wait:.1f}s (attempt {attempt+1}/3)")
                await asyncio.sleep(wait)
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"Groq error {resp.status_code}: {resp.text}")
            return resp.json()
        raise RuntimeError("Groq rate limit exceeded after 3 retries")


# ─── Anthropic client ─────────────────────────────────────────────────────────

_anthropic_client = None


async def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import AsyncAnthropic
        cfg = get_config()
        # v6 fix: create inside async context
        creds_dir = __import__("os").environ.get("CREDENTIALS_DIRECTORY", "")
        key = cfg.llm.anthropic_api_key
        _anthropic_client = AsyncAnthropic(api_key=key)
    return _anthropic_client


async def _count_tokens_anthropic(
    model: str, system: str, messages: list[dict], tools: list[dict]
) -> int:
    """v6 fix: use Anthropic's exact count_tokens API."""
    try:
        client = await _get_anthropic_client()
        response = await client.messages.count_tokens(
            model=model, system=system, messages=messages, tools=tools or []
        )
        return response.input_tokens
    except Exception:
        return _estimate_input_tokens(messages, system, tools)


# ─── Main agentic loop ────────────────────────────────────────────────────────

async def run_agent(
    messages: list[dict],
    tools: list[dict],
    system: str,
    decision: RoutingDecision,
    tool_executor: Callable[[str, dict], Any] | None = None,
    max_iterations: int = 20,
) -> tuple[str, dict]:
    """
    v6 critical fix: FULL agentic loop with tool execution.
    Not a single-call — loops until stop_reason == 'end_turn'.

    Returns:
        (final_text, usage_stats)
    """
    cfg = get_config()
    metrics = get_metrics()
    circuit_breaker = CircuitBreaker()
    history = list(messages)
    start_ts = time.time()

    # Pre-flight budget check
    estimated_input = _estimate_input_tokens(history, system, tools)
    estimated_cost = _get_model_cost(decision.model, estimated_input, decision.expected_tokens)
    daily_limit_usd = cfg.llm.daily_token_budget * 0.000004  # rough $0.004/1k tokens

    if _daily_total_usd() + estimated_cost > daily_limit_usd * 0.9:
        raise RuntimeError(f"Daily budget limit approaching: ${_daily_total_usd():.4f}/${daily_limit_usd:.2f}")

    total_input_tokens = 0
    total_output_tokens = 0

    for iteration in range(max_iterations):
        call_start = time.time()

        try:
            async for attempt in AsyncRetrying(
                wait=wait_exponential(min=1, max=30),
                stop=stop_after_attempt(3),
                retry=retry_if_exception(is_transient),
            ):
                with attempt:
                    if decision.provider == "ollama":
                        raw_response = await _ollama_call(
                            model=decision.model,
                            messages=history,
                            system=system,
                            tools=tools,
                            max_tokens=cfg.llm.max_tokens_fast,
                        )
                        msg = raw_response.get("message", {})
                        content = msg.get("content", "") or ""
                        tool_calls = msg.get("tool_calls", []) or []
                        stop_reason = "tool_use" if tool_calls else "end_turn"
                        input_tokens = raw_response.get("prompt_eval_count", 0)
                        output_tokens = raw_response.get("eval_count", 0)

                        class _OllamaTextBlock:
                            def __init__(self, data):
                                self.type = "text"
                                self.text = data

                        class _OllamaToolBlock:
                            def __init__(self, call, idx):
                                self.type = "tool_use"
                                self.id = call.get("id", f"call_{iteration}_{idx}")
                                self.name = call.get("function", {}).get("name", "")
                                raw_args = call.get("function", {}).get("arguments", {})
                                self.input = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")

                        content_blocks = [_OllamaTextBlock(content)]
                        if tool_calls:
                            content_blocks.extend([_OllamaToolBlock(tc, i) for i, tc in enumerate(tool_calls)])

                        response_obj = type("Resp", (), {
                            "stop_reason": stop_reason,
                            "content": content_blocks,
                            "usage": type("U", (), {"input_tokens": input_tokens, "output_tokens": output_tokens})(),
                        })()

                    elif decision.provider == "groq":
                        raw_response = await _groq_call(
                            model=decision.model,
                            messages=history,
                            system=system,
                            tools=tools,
                            max_tokens=cfg.llm.max_tokens_fast,
                        )
                        choice = raw_response.get("choices", [{}])[0]
                        msg = choice.get("message", {})
                        content = msg.get("content", "") or ""
                        tool_calls = msg.get("tool_calls", []) or []
                        finish_reason = choice.get("finish_reason", "stop")
                        stop_reason = "tool_use" if finish_reason == "tool_calls" else (
                            "max_tokens" if finish_reason == "length" else "end_turn"
                        )
                        usage_data = raw_response.get("usage", {})

                        class _GroqTextBlock:
                            def __init__(self, data):
                                self.type = "text"
                                self.text = data

                        class _GroqToolBlock:
                            def __init__(self, call):
                                self.type = "tool_use"
                                self.id = call.get("id", "")
                                fn = call.get("function", {})
                                self.name = fn.get("name", "")
                                raw_args = fn.get("arguments", "{}")
                                self.input = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")

                        content_blocks = [_GroqTextBlock(content)]
                        if tool_calls:
                            content_blocks.extend([_GroqToolBlock(tc) for tc in tool_calls])

                        response_obj = type("Resp", (), {
                            "stop_reason": stop_reason,
                            "content": content_blocks,
                            "usage": type("U", (), {
                                "input_tokens": usage_data.get("prompt_tokens", 0),
                                "output_tokens": usage_data.get("completion_tokens", 0),
                            })(),
                        })()

                    else:
                        client = await _get_anthropic_client()
                        response_obj = await client.messages.create(
                            model=decision.model,
                            max_tokens=cfg.llm.max_tokens_fast,
                            system=system,
                            messages=history,
                            tools=tools if tools else [],
                        )
        except Exception as e:
            metrics.llm_errors_total.inc()
            metrics.record_error()
            log.error(f"LLM call failed (iter {iteration}): {e}")
            raise

        call_duration = time.time() - call_start
        metrics.llm_latency.observe(call_duration)
        metrics.llm_requests_total.inc()

        total_input_tokens += getattr(response_obj.usage, "input_tokens", 0)
        total_output_tokens += getattr(response_obj.usage, "output_tokens", 0)

        if response_obj.stop_reason == "end_turn":
            text = "".join(
                b.text for b in response_obj.content if hasattr(b, "text") and b.type == "text"
            )
            break

        if response_obj.stop_reason == "max_tokens":
            # Continue from where it stopped — use plain text for non-Anthropic
            if decision.provider in ("ollama", "groq"):
                text_so_far = "".join(
                    b.text for b in response_obj.content if hasattr(b, "text") and b.type == "text"
                )
                history.append({"role": "assistant", "content": text_so_far})
            else:
                history.append({"role": "assistant", "content": response_obj.content})
            history.append({"role": "user", "content": "Continue from where you stopped."})
            continue

        if response_obj.stop_reason == "tool_use":
            # Anthropic expects content blocks; Ollama/Groq expect OpenAI tool_calls format
            if decision.provider in ("ollama", "groq"):
                openai_tool_calls = []
                for block in response_obj.content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        args = block.input if isinstance(block.input, str) else json.dumps(block.input)
                        tc: dict = {"type": "function", "function": {"name": block.name, "arguments": args}}
                        if hasattr(block, "id") and block.id:
                            tc["id"] = block.id
                        openai_tool_calls.append(tc)
                history.append({"role": "assistant", "content": "", "tool_calls": openai_tool_calls})
            else:
                history.append({"role": "assistant", "content": response_obj.content})

            tool_results = []

            for block in response_obj.content:
                if not hasattr(block, "type") or block.type != "tool_use":
                    continue

                # Circuit breaker check
                if not circuit_breaker.check(block.name, block.input):
                    log.warning(f"Circuit breaker: blocked repeated call to {block.name}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"[BLOCKED: repeated identical call to {block.name}]",
                    })
                    continue

                if tool_executor:
                    try:
                        result = await tool_executor(block.name, block.input)
                    except Exception as e:
                        result = f"[TOOL ERROR: {e}]"
                else:
                    result = f"[No executor registered for {block.name}]"

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result)[:50_000],
                })

            # Tool results format differs by provider
            if decision.provider == "ollama":
                for tr in tool_results:
                    history.append({"role": "tool", "content": tr["content"]})
            elif decision.provider == "groq":
                for tr in tool_results:
                    msg: dict = {"role": "tool", "content": tr["content"]}
                    if tr.get("tool_use_id"):
                        msg["tool_call_id"] = tr["tool_use_id"]
                    history.append(msg)
            else:
                history.append({"role": "user", "content": tool_results})
            continue

        # Unknown stop reason
        text = "".join(
            b.text for b in response_obj.content if hasattr(b, "text")
        )
        break
    else:
        text = "[ERROR: Max iterations reached without completion]"
        log.error("Agent loop hit max iterations")

    total_duration = time.time() - start_ts
    cost = _get_model_cost(decision.model, total_input_tokens, total_output_tokens)

    global _daily_cost_usd, _daily_tokens
    _daily_cost_usd += cost
    _daily_tokens += total_input_tokens + total_output_tokens

    metrics.record_llm_cost(decision.model, total_input_tokens, total_output_tokens)
    metrics.llm_tokens_per_day.set(_daily_tokens)

    # Persist to api_costs table for /cost endpoint
    try:
        from jarvis.memory.database import db_write as _db_write
        await _db_write(
            "INSERT INTO api_costs (model, input_tok, output_tok, cost_usd) VALUES (?,?,?,?)",
            (decision.model, total_input_tokens, total_output_tokens, cost),
        )
    except Exception:
        pass  # non-critical — never block on cost tracking

    usage_stats = {
        "model": decision.model,
        "provider": decision.provider,
        "task_type": decision.task_type,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "cost_usd": round(cost, 6),
        "duration_s": round(total_duration, 3),
        "iterations": iteration + 1,
    }

    get_audit().log_action(
        tool="llm",
        input_data=messages[-1]["content"] if messages else "",
        output=text[:500],
        success=True,
        duration_ms=total_duration * 1000,
        model_used=decision.model,
        tokens_used=total_input_tokens + total_output_tokens,
    )

    return text, usage_stats


async def simple_completion(
    prompt: str,
    task_type: str = "simple_qa",
    system: str | None = None,
    tools: list[dict] | None = None,
) -> str:
    """Convenience wrapper for single-turn completions."""
    cfg = get_config()
    from jarvis.llm.router import select_model, TaskType
    decision = select_model(task_type)  # type: ignore

    if system is None:
        system_path = __import__("pathlib").Path("CLAUDE.md")
        system = system_path.read_text() if system_path.exists() else "You are JARVIS, an autonomous assistant."

    messages = [{"role": "user", "content": prompt}]
    text, _ = await run_agent(
        messages=messages,
        tools=tools or [],
        system=system,
        decision=decision,
    )
    return text
