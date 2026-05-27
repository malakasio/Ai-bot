"""
Model routing — determines which LLM to use for each task.
Ported from src/jarvis/llm/router.py to core/ architecture.

Strategy:
1. Rule-based keyword routing (0ms, 0 cost)
2. Task-aware model selection (Haiku for simple, Sonnet for code, Opus for architecture)

Provider priority:
- LiteLLM proxy (localhost:4000) — primary routing layer
- Anthropic/NeutralBeats (Claude) — if ANTHROPIC_API_KEY set, direct
- Groq (free cloud) — if GROQ_API_KEY set
- Ollama (local) — fallback

Environment variables:
- ANTHROPIC_API_KEY: Enable Claude models
- GROQ_API_KEY: Enable Groq free tier
- LITELLM_BASE_URL: LiteLLM proxy endpoint (default: http://localhost:4000)
- OLLAMA_BASE_URL: Ollama endpoint (default: http://localhost:11434)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

TaskType = Literal[
    "simple_qa",
    "voice",
    "notification",
    "monitoring",
    "code_review",
    "code_generation",
    "analysis",
    "summarization",
    "system_mgmt",
    "architecture",
    "deep_debug",
    "critical",
]

ModelTier = Literal["local_fast", "local_smart", "paid_fast", "paid_smart", "paid_heavy"]


@dataclass
class RoutingDecision:
    task_type: TaskType
    tier: ModelTier
    model: str
    provider: str
    reason: str
    expected_tokens: int


# Expected output token counts by task type
EXPECTED_OUTPUT_TOKENS: dict[TaskType, int] = {
    "simple_qa": 256,
    "voice": 150,
    "notification": 100,
    "monitoring": 128,
    "code_review": 2048,
    "code_generation": 4096,
    "analysis": 2048,
    "summarization": 1024,
    "system_mgmt": 512,
    "architecture": 4096,
    "deep_debug": 4096,
    "critical": 8192,
}

# Keyword → task type mapping
KEYWORD_ROUTES: list[tuple[list[str], TaskType]] = [
    # Voice/quick
    (["ping", "hello", "ok", "thanks", "time", "date", "weather"], "simple_qa"),
    (["notification", "reminder", "alert"], "notification"),
    # Code
    (
        ["code", "python", "javascript", "bug", "error", "script", "debug", "fix", "refactor"],
        "code_review",
    ),
    (
        ["write", "create", "implement", "function", "class", "module", "generate"],
        "code_generation",
    ),
    # Analysis
    (["analyze", "logs", "report", "summary", "stats"], "analysis"),
    (["summarize", "compress", "tldr"], "summarization"),
    # System
    (["systemd", "service", "restart", "daemon", "process", "server", "deploy"], "system_mgmt"),
    (["monitor", "health", "status", "uptime"], "monitoring"),
    # Architecture/Heavy
    (["architecture", "design", "plan", "blueprint"], "architecture"),
    (["deep debug", "root cause", "trace", "profile", "performance issue"], "deep_debug"),
    (["critical", "production down", "emergency", "urgent fix"], "critical"),
]


def classify_task_by_keywords(text: str) -> TaskType:
    """Rule-based classification — 0ms, 0 cost."""
    text_lower = text.lower()
    for keywords, task_type in KEYWORD_ROUTES:
        if any(kw in text_lower for kw in keywords):
            return task_type
    return "simple_qa"


def select_model(task_type: TaskType) -> RoutingDecision:
    """
    Concrete model selection logic.
    Prioritizes FREE local models; uses paid only if API key set.
    """
    expected_tokens = EXPECTED_OUTPUT_TOKENS.get(task_type, 1024)

    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_groq = bool(os.environ.get("GROQ_API_KEY"))

    # Groq (FREE cloud) — use when available and no Anthropic key
    if has_groq and not has_anthropic:
        model = (
            "llama-3.3-70b-versatile"
            if task_type
            in (
                "architecture",
                "deep_debug",
                "critical",
                "code_generation",
                "code_review",
                "analysis",
            )
            else "llama-3.1-8b-instant"
        )
        return RoutingDecision(
            task_type=task_type,
            tier="local_fast",
            model=model,
            provider="groq",
            reason="free Groq cloud LLM",
            expected_tokens=expected_tokens,
        )

    # Claude routing (if ANTHROPIC_API_KEY set)
    if has_anthropic:
        # Haiku 4.5: fast tasks
        if task_type in ("simple_qa", "voice", "notification", "monitoring", "summarization"):
            return RoutingDecision(
                task_type=task_type,
                tier="paid_fast",
                model="claude-haiku-4-5",
                provider="anthropic",
                reason="Claude Haiku: fast + cheap for simple tasks",
                expected_tokens=expected_tokens,
            )
        # Sonnet 4.6: balanced — code, analysis
        if task_type in ("code_review", "code_generation", "analysis", "system_mgmt"):
            return RoutingDecision(
                task_type=task_type,
                tier="paid_smart",
                model="claude-sonnet-4-6",
                provider="anthropic",
                reason="Claude Sonnet: best speed/intelligence for code & analysis",
                expected_tokens=expected_tokens,
            )
        # Opus 4.7: most capable — architecture, deep debugging, critical
        return RoutingDecision(
            task_type=task_type,
            tier="paid_heavy",
            model="claude-opus-4-7",
            provider="anthropic",
            reason="Claude Opus: maximum capability for complex tasks",
            expected_tokens=expected_tokens,
        )

    # Ollama fallback (local, free)
    os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    if task_type in ("simple_qa", "voice", "notification", "monitoring"):
        return RoutingDecision(
            task_type=task_type,
            tier="local_fast",
            model="llama3.2:3b",
            provider="ollama",
            reason="local fast model",
            expected_tokens=expected_tokens,
        )

    if task_type in ("code_review", "analysis", "system_mgmt", "summarization", "code_generation"):
        return RoutingDecision(
            task_type=task_type,
            tier="local_smart",
            model="qwen2.5-coder:7b",
            provider="ollama",
            reason="local smart model",
            expected_tokens=expected_tokens,
        )

    if task_type in ("architecture", "deep_debug", "critical"):
        return RoutingDecision(
            task_type=task_type,
            tier="local_smart",
            model="qwen2.5-coder:14b",
            provider="ollama",
            reason="local smart model (no Claude key)",
            expected_tokens=expected_tokens,
        )

    # Default
    return RoutingDecision(
        task_type=task_type,
        tier="local_fast",
        model="llama3.2:3b",
        provider="ollama",
        reason="default local fast",
        expected_tokens=expected_tokens,
    )


def route(text: str) -> RoutingDecision:
    """Full routing pipeline: classify → select model."""
    task_type = classify_task_by_keywords(text)
    decision = select_model(task_type)
    return decision
