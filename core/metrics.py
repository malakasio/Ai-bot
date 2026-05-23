"""
In-process metrics collection with Prometheus-compatible exposition.
Ported from src/jarvis/observability/metrics.py to core/ architecture.

Tracks:
- LLM requests, tokens, costs, latency
- Voice sessions, STT/TTS latency, barge-ins
- Agent tasks, scores, circuit breaker trips
- Memory operations
- KAIROS daemon runs
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class Counter:
    name: str
    help: str
    _value: float = 0.0

    def inc(self, amount: float = 1.0):
        self._value += amount

    @property
    def value(self) -> float:
        return self._value


@dataclass
class Gauge:
    name: str
    help: str
    _value: float = 0.0

    def set(self, v: float):
        self._value = v

    def inc(self, amount: float = 1.0):
        self._value += amount

    def dec(self, amount: float = 1.0):
        self._value -= amount

    @property
    def value(self) -> float:
        return self._value


@dataclass
class Histogram:
    name: str
    help: str
    buckets: list[float] = field(default_factory=lambda: [0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0])
    _observations: list[float] = field(default_factory=list)

    def observe(self, value: float):
        self._observations.append(value)
        if len(self._observations) > 10_000:
            self._observations = self._observations[-5_000:]

    @property
    def count(self) -> int:
        return len(self._observations)

    @property
    def sum(self) -> float:
        return sum(self._observations)

    @property
    def p50(self) -> float:
        if not self._observations:
            return 0.0
        s = sorted(self._observations)
        return s[len(s) // 2]

    @property
    def p95(self) -> float:
        if not self._observations:
            return 0.0
        s = sorted(self._observations)
        return s[int(len(s) * 0.95)]

    @property
    def p99(self) -> float:
        if not self._observations:
            return 0.0
        s = sorted(self._observations)
        return s[int(len(s) * 0.99)]


class MetricsRegistry:
    """Central registry of all metrics."""

    def __init__(self):
        # LLM metrics
        self.llm_requests_total = Counter("jarvis_llm_requests_total", "Total LLM requests")
        self.llm_errors_total = Counter("jarvis_llm_errors_total", "Total LLM errors")
        self.llm_tokens_total = Counter("jarvis_llm_tokens_total", "Total tokens consumed")
        self.llm_cost_usd = Counter("jarvis_llm_cost_usd", "Estimated LLM cost in USD")
        self.llm_latency = Histogram("jarvis_llm_latency_seconds", "LLM response latency")

        # Voice metrics
        self.voice_sessions_total = Counter("jarvis_voice_sessions_total", "Total voice sessions")
        self.voice_latency = Histogram("jarvis_voice_e2e_latency_seconds", "End-to-end voice latency")
        self.stt_latency = Histogram("jarvis_stt_latency_seconds", "STT latency")
        self.tts_latency = Histogram("jarvis_tts_latency_seconds", "TTS latency")
        self.barge_in_total = Counter("jarvis_barge_in_total", "Total barge-in events")

        # Agent metrics
        self.tasks_total = Counter("jarvis_tasks_total", "Total tasks processed")
        self.tasks_success = Counter("jarvis_tasks_success", "Successful tasks")
        self.tasks_failed = Counter("jarvis_tasks_failed", "Failed tasks")
        self.task_score = Histogram("jarvis_task_score", "Task quality score (0-100)")
        self.circuit_breaker_trips = Counter("jarvis_circuit_breaker_trips", "Circuit breaker activations")

        # Memory metrics
        self.memory_writes = Counter("jarvis_memory_writes_total", "Memory write operations")
        self.memory_reads = Counter("jarvis_memory_reads_total", "Memory read operations")
        self.memory_size = Gauge("jarvis_memory_records", "Total memory records")

        # System metrics
        self.uptime_seconds = Gauge("jarvis_uptime_seconds", "Agent uptime")
        self.kairos_runs = Counter("jarvis_kairos_runs_total", "KAIROS daemon runs")

        self._start_time = time.time()
        self._recent_errors: deque[float] = deque(maxlen=100)

    def record_error(self):
        self._recent_errors.append(time.time())

    @property
    def error_rate_1h(self) -> float:
        cutoff = time.time() - 3600
        recent = sum(1 for t in self._recent_errors if t > cutoff)
        return recent / max(1.0, self.tasks_total.value) * 100

    def record_llm_cost(self, model: str, input_tokens: int, output_tokens: int):
        """Estimate and record LLM API cost."""
        costs = {
            "claude-haiku": (0.00025, 0.00125),
            "claude-sonnet": (0.003, 0.015),
            "claude-opus": (0.015, 0.075),
            "gpt-4o-mini": (0.00015, 0.0006),
            "ollama": (0.0, 0.0),
            "groq": (0.0, 0.0),  # Free tier
        }
        prefix = next((k for k in costs if k in model.lower()), "ollama")
        input_cost, output_cost = costs[prefix]
        total_cost = (input_tokens / 1000) * input_cost + (output_tokens / 1000) * output_cost
        self.llm_cost_usd.inc(total_cost)
        self.llm_tokens_total.inc(input_tokens + output_tokens)

    def export_prometheus(self) -> str:
        """Export metrics in Prometheus text format."""
        lines = []

        # Update uptime
        self.uptime_seconds.set(time.time() - self._start_time)

        # Export all metrics
        for attr_name in dir(self):
            attr = getattr(self, attr_name)
            if isinstance(attr, Counter):
                lines.append(f"# HELP {attr.name} {attr.help}")
                lines.append(f"# TYPE {attr.name} counter")
                lines.append(f"{attr.name} {attr.value}")
            elif isinstance(attr, Gauge):
                lines.append(f"# HELP {attr.name} {attr.help}")
                lines.append(f"# TYPE {attr.name} gauge")
                lines.append(f"{attr.name} {attr.value}")
            elif isinstance(attr, Histogram):
                lines.append(f"# HELP {attr.name} {attr.help}")
                lines.append(f"# TYPE {attr.name} histogram")
                lines.append(f"{attr.name}_count {attr.count}")
                lines.append(f"{attr.name}_sum {attr.sum}")
                lines.append(f"{attr.name}_p50 {attr.p50}")
                lines.append(f"{attr.name}_p95 {attr.p95}")
                lines.append(f"{attr.name}_p99 {attr.p99}")

        return "\n".join(lines) + "\n"


# Global singleton
_metrics: MetricsRegistry | None = None


def get_metrics() -> MetricsRegistry:
    """Get or create the global metrics registry."""
    global _metrics
    if _metrics is None:
        _metrics = MetricsRegistry()
    return _metrics
