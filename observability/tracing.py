"""Structured tracing, OpenTelemetry instrumentation, and task evaluation.

Three concerns live here, all behind one import:

  1. Trace emission. ``Tracer.event(kind, **fields)`` appends one JSON
     object per line to /var/log/jarvis_agent.trace (with a $HOME
     fallback). Reuses the agent's RotatingFileHandler so we do not
     stomp on its rotation policy.

  2. OpenTelemetry instrumentation. ``instrument_llm_call(...)`` /
     ``instrument_tool_call(...)`` are context managers that open an
     OTel span around the body. When OTel isn't installed they degrade
     to a no-op span that still emits the same trace event — the rest
     of the system never branches on "is OTel here".

  3. Evaluation. ``score_task(...)`` turns a TaskExecution into an
     integer score (0..100) using duration, token cost, and test
     success. ``persist_score()`` writes the score + breakdown into
     PostgreSQL ``metrics`` (the table is created on first call when
     core.database is available).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator, Optional


# ─── Constants ────────────────────────────────────────────────────────────

TRACE_LOG_PATH = Path("/var/log/jarvis_agent.trace")
TRACE_LOG_FALLBACK = Path.home() / ".local/share/jarvis/jarvis_agent.trace"

# Default scoring weights — tune via env without touching code.
SCORE_BASE = 100
W_DURATION_S_PER_POINT = float(os.environ.get(
    "JARVIS_SCORE_W_DURATION_S", "5.0"))     # 1 point lost per 5s
W_TOKENS_PER_POINT = float(os.environ.get(
    "JARVIS_SCORE_W_TOKENS", "1000.0"))      # 1 point lost per 1k tokens
W_EXCEPTION_PENALTY = int(os.environ.get(
    "JARVIS_SCORE_W_EXCEPTION", "30"))
W_TEST_FAIL_PENALTY = int(os.environ.get(
    "JARVIS_SCORE_W_TEST_FAIL", "40"))
W_BUDGET_OVERRUN_PENALTY = int(os.environ.get(
    "JARVIS_SCORE_W_BUDGET", "20"))
SCORE_FLOOR = 0


# ─── Logger that writes JSONL to the trace file ───────────────────────────


class _JsonLineFormatter(logging.Formatter):
    """One JSON object per line; merges record.extra into the payload."""

    _BUILTIN_FIELDS = frozenset({
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
    })

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k in self._BUILTIN_FIELDS:
                continue
            try:
                json.dumps(v)
                out[k] = v
            except (TypeError, ValueError):
                out[k] = repr(v)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def _resolve_trace_path() -> Path:
    target = TRACE_LOG_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8"):
            pass
        return target
    except (PermissionError, OSError):
        TRACE_LOG_FALLBACK.parent.mkdir(parents=True, exist_ok=True)
        return TRACE_LOG_FALLBACK


_logger: Optional[logging.Logger] = None
_trace_path: Optional[Path] = None


def get_logger() -> logging.Logger:
    """Lazy init. Reuses core.agent's logger when available so the trace
    file is shared."""
    global _logger, _trace_path
    if _logger is not None:
        return _logger
    # Prefer the agent's existing logger to avoid two handlers writing
    # to the same file.
    try:
        from core import agent as _agent  # noqa: F401
        _logger = _agent.get_logger()
        return _logger
    except Exception:
        pass

    log = logging.getLogger("jarvis.tracing")
    log.setLevel(logging.INFO)
    log.propagate = False
    _trace_path = _resolve_trace_path()
    handler = logging.handlers.RotatingFileHandler(
        _trace_path,
        maxBytes=int(os.environ.get("JARVIS_TRACE_ROTATE_BYTES",
                                    50 * 1024 * 1024)),
        backupCount=int(os.environ.get("JARVIS_TRACE_KEEP", 5)),
        encoding="utf-8",
    )
    handler.setFormatter(_JsonLineFormatter())
    log.addHandler(handler)
    if os.environ.get("JARVIS_TRACE_STDERR") == "1":
        sh = logging.StreamHandler()
        sh.setFormatter(_JsonLineFormatter())
        log.addHandler(sh)
    _logger = log
    return log


def trace_path() -> Path:
    """The actual path the trace logger is writing to."""
    if _trace_path is not None:
        return _trace_path
    return _resolve_trace_path()


# ─── OpenTelemetry shim ───────────────────────────────────────────────────


_otel_tracer: Any = None
_otel_initialized = False


def _otel_init() -> Any:
    """Return an OTel tracer or None. Idempotent."""
    global _otel_tracer, _otel_initialized
    if _otel_initialized:
        return _otel_tracer
    _otel_initialized = True

    try:
        from opentelemetry import trace as _trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor, ConsoleSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        return None  # OTel not installed; that's fine.

    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "jarvis"),
        "service.version": os.environ.get("JARVIS_VERSION", "7.0.0"),
    })
    provider = TracerProvider(resource=resource)

    # OTLP exporter if endpoint configured; console exporter otherwise.
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            )
        except ImportError:
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        # Quiet by default: only attach console exporter if explicitly asked.
        if os.environ.get("OTEL_CONSOLE", "").lower() in {"1", "true", "yes"}:
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    _trace.set_tracer_provider(provider)
    _otel_tracer = _trace.get_tracer("jarvis", os.environ.get("JARVIS_VERSION", "7.0.0"))
    return _otel_tracer


@contextlib.contextmanager
def _otel_span(name: str, attrs: dict[str, Any]) -> Iterator[Any]:
    tracer = _otel_init()
    if tracer is None:
        # No-op fallback; same `as span` shape so callers don't branch.
        class _Noop:
            def set_attribute(self, k, v): pass
            def record_exception(self, e): pass
            def set_status(self, *_a, **_kw): pass
        yield _Noop()
        return
    with tracer.start_as_current_span(name) as span:
        for k, v in attrs.items():
            try:
                span.set_attribute(k, v)
            except Exception:
                span.set_attribute(k, repr(v))
        try:
            yield span
        except Exception as e:
            try:
                span.record_exception(e)
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR, repr(e)))
            except Exception:
                pass
            raise


# ─── Trace API ────────────────────────────────────────────────────────────


class Tracer:
    """High-level event emitter. Used like:

        tr = Tracer.get()
        tr.event("tool_use", tool="filesystem.write_file", input_keys=[...])
    """

    _instance: Optional["Tracer"] = None

    @classmethod
    def get(cls) -> "Tracer":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self.log = get_logger()

    def event(self, _event_name: str, /, **fields: Any) -> None:
        self.log.info(_event_name, extra=fields)

    def exception(self, _event_name: str, exc: BaseException, /, **fields: Any) -> None:
        merged = {"exc": repr(exc), **fields}
        self.log.error(_event_name, extra=merged,
                       exc_info=(type(exc), exc, exc.__traceback__))


def event(_event_name: str, /, **fields: Any) -> None:
    """Module-level shortcut. The event name is positional-only so callers
    can freely pass ``kind=`` / ``tool=`` / ``model=`` as fields."""
    Tracer.get().event(_event_name, **fields)


# ─── Instrumentation: LLM + tool wrappers ─────────────────────────────────


@contextlib.contextmanager
def instrument_llm_call(
    *,
    model: str,
    run_id: Optional[str] = None,
    iteration: Optional[int] = None,
    estimated_input_tokens: Optional[int] = None,
) -> Iterator[dict[str, Any]]:
    """Context manager that emits llm.start / llm.end events and an OTel
    span. The yielded dict accepts updates the caller computes inside the
    block (e.g. real input/output token counts) before the .end event."""
    span_attrs = {
        "llm.model": model,
        "llm.run_id": run_id or "",
        "llm.iteration": iteration if iteration is not None else -1,
        "llm.est_input_tokens": estimated_input_tokens or 0,
    }
    record: dict[str, Any] = {
        "model": model, "run_id": run_id, "iteration": iteration,
        "estimated_input_tokens": estimated_input_tokens,
        "input_tokens": None, "output_tokens": None,
        "exception": None,
    }
    t0 = time.monotonic()
    event("llm.start", **record)
    with _otel_span("llm.call", span_attrs) as span:
        try:
            yield record
        except BaseException as e:
            record["exception"] = repr(e)
            event("llm.exception", **record)
            raise
        finally:
            record["duration_ms"] = int((time.monotonic() - t0) * 1000)
            if span is not None:
                try:
                    if record.get("input_tokens") is not None:
                        span.set_attribute("llm.input_tokens", int(record["input_tokens"]))
                    if record.get("output_tokens") is not None:
                        span.set_attribute("llm.output_tokens", int(record["output_tokens"]))
                    span.set_attribute("llm.duration_ms", record["duration_ms"])
                except Exception:
                    pass
            event("llm.end", **record)


@contextlib.contextmanager
def instrument_tool_call(
    *,
    tool: str,
    is_destructive: bool = False,
    snapshot_tag: Optional[str] = None,
) -> Iterator[dict[str, Any]]:
    """Context manager for tool dispatch. Captures duration, exception,
    and the resulting snapshot tag (if any)."""
    record: dict[str, Any] = {
        "tool": tool, "destructive": is_destructive,
        "snapshot": snapshot_tag,
        "exception": None,
    }
    t0 = time.monotonic()
    event("tool_use.start", **record)
    with _otel_span("tool.call",
                    {"tool.name": tool, "tool.destructive": is_destructive}) as span:
        try:
            yield record
        except BaseException as e:
            record["exception"] = repr(e)
            event("tool_use.exception", **record)
            raise
        finally:
            record["duration_ms"] = int((time.monotonic() - t0) * 1000)
            if span is not None:
                try:
                    span.set_attribute("tool.duration_ms", record["duration_ms"])
                    if record.get("snapshot"):
                        span.set_attribute("tool.snapshot", record["snapshot"])
                except Exception:
                    pass
            event("tool_use.end", **record)


def record_mutation(*, kind: str, target: str, snapshot_tag: Optional[str] = None,
                    metadata: Optional[dict[str, Any]] = None) -> None:
    """Emit a `mutation` trace event for any filesystem / DB / system
    change. snapshot_tag is the pre-mutation tag (if a snapshot was taken)."""
    event("mutation", kind=kind, target=target,
          snapshot=snapshot_tag, metadata=metadata or {})


def record_exception(where: str, exc: BaseException, **fields: Any) -> None:
    Tracer.get().exception("exception", exc, where=where, **fields)


# ─── Evaluation layer ─────────────────────────────────────────────────────


@dataclass
class TaskExecution:
    """Inputs to the scorer. Pass what you measured; missing values are
    treated as zero / unknown rather than raising."""
    task_id: str
    actor: str = "jarvis"
    tool: str = ""
    duration_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    token_budget: int = 0          # 0 means "no budget configured"
    exit_code: Optional[int] = None
    exceptions: int = 0
    tests_total: int = 0
    tests_passed: int = 0
    notes: str = ""


@dataclass
class ScoreBreakdown:
    score: int
    base: int = SCORE_BASE
    duration_penalty: int = 0
    token_penalty: int = 0
    exception_penalty: int = 0
    test_fail_penalty: int = 0
    budget_overrun_penalty: int = 0
    reasons: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def score_task(ex: TaskExecution) -> ScoreBreakdown:
    """Deterministic 0-100 score based on cost, errors, and test success.

    Heuristic but stable; documented above as scoring weights so we can
    tune without rewriting callers.
    """
    breakdown = ScoreBreakdown(score=SCORE_BASE)

    if W_DURATION_S_PER_POINT > 0 and ex.duration_s > 0:
        d = int(ex.duration_s / W_DURATION_S_PER_POINT)
        breakdown.duration_penalty = d
        if d:
            breakdown.reasons.append(f"-{d} for {ex.duration_s:.1f}s duration")

    total_tokens = (ex.input_tokens or 0) + (ex.output_tokens or 0)
    if W_TOKENS_PER_POINT > 0 and total_tokens > 0:
        t = int(total_tokens / W_TOKENS_PER_POINT)
        breakdown.token_penalty = t
        if t:
            breakdown.reasons.append(f"-{t} for {total_tokens} tokens")

    if ex.exceptions:
        p = ex.exceptions * W_EXCEPTION_PENALTY
        breakdown.exception_penalty = p
        breakdown.reasons.append(f"-{p} for {ex.exceptions} exception(s)")

    if ex.exit_code not in (None, 0):
        breakdown.exception_penalty += W_EXCEPTION_PENALTY
        breakdown.reasons.append(f"-{W_EXCEPTION_PENALTY} for exit_code={ex.exit_code}")

    if ex.tests_total > 0 and ex.tests_passed < ex.tests_total:
        failed = ex.tests_total - ex.tests_passed
        p = min(W_TEST_FAIL_PENALTY, int(W_TEST_FAIL_PENALTY * failed / ex.tests_total))
        breakdown.test_fail_penalty = p
        breakdown.reasons.append(
            f"-{p} for {failed}/{ex.tests_total} tests failed"
        )

    if ex.token_budget > 0 and total_tokens > ex.token_budget:
        breakdown.budget_overrun_penalty = W_BUDGET_OVERRUN_PENALTY
        breakdown.reasons.append(
            f"-{W_BUDGET_OVERRUN_PENALTY} for tokens {total_tokens} > budget {ex.token_budget}"
        )

    total = (breakdown.base
             - breakdown.duration_penalty
             - breakdown.token_penalty
             - breakdown.exception_penalty
             - breakdown.test_fail_penalty
             - breakdown.budget_overrun_penalty)
    breakdown.score = max(SCORE_FLOOR, min(100, total))
    return breakdown


# ─── Metrics persistence ──────────────────────────────────────────────────


_METRICS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metrics (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    task_id         TEXT NOT NULL,
    actor           TEXT NOT NULL,
    tool            TEXT,
    score           SMALLINT NOT NULL,
    duration_s      REAL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    token_budget    INTEGER,
    exit_code       INTEGER,
    exceptions      INTEGER,
    tests_total     INTEGER,
    tests_passed    INTEGER,
    breakdown       JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS metrics_ts_idx ON metrics (ts DESC);
CREATE INDEX IF NOT EXISTS metrics_task_idx ON metrics (task_id);
CREATE INDEX IF NOT EXISTS metrics_actor_tool_idx ON metrics (actor, tool, ts DESC);
"""

_metrics_table_ready = False


async def ensure_metrics_schema() -> bool:
    """Create the metrics table once per process. True iff the DB is reachable."""
    global _metrics_table_ready
    if _metrics_table_ready:
        return True
    try:
        from core import database
        await database.execute(_METRICS_SCHEMA_SQL)
        _metrics_table_ready = True
        return True
    except Exception as e:
        event("metrics.schema_init_failed", exc=repr(e))
        return False


async def persist_score(ex: TaskExecution, breakdown: ScoreBreakdown
                        ) -> Optional[int]:
    """Insert one row into ``metrics``. Returns the row id or None on
    failure. Best-effort: failures are logged, not raised."""
    if not await ensure_metrics_schema():
        return None
    try:
        from core import database
        row = await database.fetchrow(
            """
            INSERT INTO metrics (task_id, actor, tool, score, duration_s,
                                 input_tokens, output_tokens, token_budget,
                                 exit_code, exceptions, tests_total,
                                 tests_passed, breakdown, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                    $13::jsonb, $14)
            RETURNING id
            """,
            ex.task_id, ex.actor, ex.tool, breakdown.score,
            ex.duration_s, ex.input_tokens, ex.output_tokens,
            ex.token_budget, ex.exit_code, ex.exceptions,
            ex.tests_total, ex.tests_passed,
            json.dumps(breakdown.asdict()), ex.notes,
        )
        rid = int(row["id"])
        event("metrics.persisted", id=rid, task_id=ex.task_id,
              score=breakdown.score)
        return rid
    except Exception as e:
        event("metrics.persist_failed", task_id=ex.task_id, exc=repr(e))
        return None


async def evaluate_and_persist(ex: TaskExecution) -> ScoreBreakdown:
    """Convenience: score + persist + emit event. Returns the breakdown."""
    breakdown = score_task(ex)
    event("evaluation",
          task_id=ex.task_id, score=breakdown.score,
          reasons=breakdown.reasons,
          duration_s=ex.duration_s, total_tokens=(ex.input_tokens + ex.output_tokens),
          exceptions=ex.exceptions,
          tests=(ex.tests_passed, ex.tests_total))
    await persist_score(ex, breakdown)
    return breakdown


# ─── CLI for ad-hoc inspection ────────────────────────────────────────────


def _main() -> None:  # pragma: no cover
    import argparse
    parser = argparse.ArgumentParser(prog="observability.tracing")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_tail = sub.add_parser("tail", help="print the trace path and tail it")
    p_tail.add_argument("-n", type=int, default=20)

    p_score = sub.add_parser("score", help="score a hypothetical execution")
    p_score.add_argument("--duration-s", type=float, default=10.0)
    p_score.add_argument("--input-tokens", type=int, default=5000)
    p_score.add_argument("--output-tokens", type=int, default=2000)
    p_score.add_argument("--exceptions", type=int, default=0)
    p_score.add_argument("--tests-total", type=int, default=0)
    p_score.add_argument("--tests-passed", type=int, default=0)

    args = parser.parse_args()

    if args.cmd == "tail":
        p = trace_path()
        print(f"# trace: {p}")
        try:
            with open(p, "r", encoding="utf-8") as f:
                lines = f.readlines()[-args.n:]
            for line in lines:
                print(line.rstrip())
        except FileNotFoundError:
            print("# (no trace yet)")
        return

    if args.cmd == "score":
        ex = TaskExecution(
            task_id="cli", actor="dev", tool="cli",
            duration_s=args.duration_s,
            input_tokens=args.input_tokens,
            output_tokens=args.output_tokens,
            exceptions=args.exceptions,
            tests_total=args.tests_total,
            tests_passed=args.tests_passed,
        )
        b = score_task(ex)
        print(json.dumps(b.asdict(), indent=2))


if __name__ == "__main__":  # pragma: no cover
    _main()
