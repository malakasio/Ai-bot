"""KAIROS — background task scheduler and idle-time memory consolidator.

Runs a single tick every KAIROS_INTERVAL seconds (default 60). One tick:

  1. Drain at most KAIROS_TASK_BATCH tasks from the PostgreSQL queue
     (core.memory.claim_next_task / complete_task) and dispatch them.
  2. Poll watched GitHub repos for new commits/issues/PRs and emit
     Telegram notifications + episodes for material changes.
  3. Run a system health check (disk, memory, CPU). Alert on thresholds.
  4. If the system has been idle for more than DREAM_IDLE_THRESHOLD
     seconds, run auto_dream() to consolidate episodes into semantic
     memory.

All steps are best-effort and isolated: an exception in one does not
abort the rest of the tick. Every meaningful event is written back as an
episode through core.memory.store_episode so the system has a record of
what KAIROS did.

Configuration (env):
  KAIROS_INTERVAL              default 60
  KAIROS_TASK_BATCH            default 5
  KAIROS_GITHUB_REPOS          comma-separated "owner/repo"
  KAIROS_GITHUB_TOKEN          optional GitHub PAT (raises rate limit)
  KAIROS_GITHUB_POLL_EVERY_N   poll repos every N ticks (default 5)
  KAIROS_HEALTH_DISK_PCT       alert above N % used (default 90)
  KAIROS_HEALTH_MEM_PCT        alert above N % used (default 90)
  KAIROS_HEALTH_CPU_PCT        alert above N % 1-min average (default 95)
  DREAM_IDLE_THRESHOLD         seconds (default 900 = 15 min)
  DREAM_LOOKBACK_HOURS         hours of episodes to scan (default 24)
  DREAM_MAX_EPISODES           cap on episodes per pass (default 500)
  KAIROS_DRY_RUN               true => no DB writes, no Telegram pushes
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional


# ─── Constants / config ───────────────────────────────────────────────────


DEFAULT_INTERVAL_S = 60
DEFAULT_TASK_BATCH = 5
DEFAULT_GITHUB_POLL_EVERY_N = 5
DEFAULT_DREAM_IDLE_S = 15 * 60
DEFAULT_DREAM_LOOKBACK_HOURS = 24
DEFAULT_DREAM_MAX_EPISODES = 500
DEFAULT_HEALTH_DISK_PCT = 90
DEFAULT_HEALTH_MEM_PCT = 90
DEFAULT_HEALTH_CPU_PCT = 95


def _int_env(name: str, default: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v) if v is not None and v != "" else default
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _list_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


# ─── Logger ───────────────────────────────────────────────────────────────


def _get_logger():
    try:
        from . import agent as _agent
        return _agent.get_logger()
    except Exception:
        import logging
        log = logging.getLogger("jarvis.kairos")
        if not log.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            log.addHandler(h)
            log.setLevel(logging.INFO)
        return log


async def _telegram(text: str) -> bool:
    """Send a Telegram notification via the agent helper if available."""
    try:
        from . import agent as _agent
        return await _agent.send_telegram_alert(text)
    except Exception:
        pass
    # Fallback: minimal httpx call.
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (os.environ.get("TELEGRAM_USER_ID")
               or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text,
                      "disable_web_page_preview": True},
            )
        return r.status_code == 200
    except Exception:
        return False


# ─── Episode helper (best-effort, never raises) ───────────────────────────


async def _emit_episode(actor: str, tool: str, *, input_: str = "",
                        output: str = "", exit_code: int = 0,
                        zone: str = "green", score: Optional[int] = None,
                        metadata: Optional[dict[str, Any]] = None) -> None:
    if _bool_env("KAIROS_DRY_RUN", False):
        return
    try:
        from . import memory
        await memory.store_episode(memory.Episode(
            actor=actor, tool=tool, input=input_, output=output,
            exit_code=exit_code, zone=zone, score=score,
            metadata=metadata or {},
        ))
    except Exception as e:  # pragma: no cover
        _get_logger().warning("kairos.episode.write_failed",
                              extra={"exc": repr(e)})


# ─── 1) Task queue drain ──────────────────────────────────────────────────


# Registered handlers for the queue. Each is awaitable: handler(target, payload).
_TASK_HANDLERS: dict[str, Callable[..., Any]] = {}


def register_task_handler(kind: str, handler: Callable[..., Any]) -> None:
    _TASK_HANDLERS[kind] = handler


async def _drain_task_queue(batch: int) -> dict[str, Any]:
    """Claim up to `batch` queued tasks and dispatch each."""
    log = _get_logger()
    out = {"claimed": 0, "done": 0, "failed": 0, "skipped_no_handler": 0}
    if _bool_env("KAIROS_DRY_RUN", False):
        out["dry_run"] = True
        return out
    try:
        from . import memory
    except Exception as e:
        out["error"] = f"memory_import_failed: {e!r}"
        return out

    for _ in range(max(1, batch)):
        try:
            task = await memory.claim_next_task("kairos")
        except Exception as e:
            out["error"] = f"claim_failed: {e!r}"
            break
        if task is None:
            break
        out["claimed"] += 1
        log.info("kairos.task.claim", extra={
            "task_id": str(task.id), "kind": task.kind, "target": task.target,
            "attempt": task.attempts,
        })
        handler = _TASK_HANDLERS.get(task.kind)
        if handler is None:
            out["skipped_no_handler"] += 1
            try:
                await memory.complete_task(
                    task.id, error=f"no handler for kind={task.kind}")
            except Exception:
                pass
            continue
        try:
            result = await handler(task.target, task.payload or {})
            await memory.complete_task(task.id, result=result if isinstance(result, dict) else {"result": result})
            out["done"] += 1
        except Exception as e:
            out["failed"] += 1
            log.warning("kairos.task.fail", extra={
                "task_id": str(task.id), "exc": repr(e)})
            try:
                await memory.complete_task(task.id, error=repr(e))
            except Exception:
                pass
    return out


# ─── 2) GitHub poll ───────────────────────────────────────────────────────


_LAST_SEEN_COMMIT: dict[str, str] = {}


async def _poll_github() -> dict[str, Any]:
    repos = _list_env("KAIROS_GITHUB_REPOS")
    if not repos:
        return {"skipped": "no_repos"}
    try:
        import httpx
    except ImportError:
        return {"skipped": "missing_httpx"}
    token = os.environ.get("KAIROS_GITHUB_TOKEN", "").strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "jarvis-kairos/7.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    results = []
    log = _get_logger()
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as c:
        for repo in repos:
            if not re.fullmatch(r"[A-Za-z0-9._\-]+/[A-Za-z0-9._\-]+", repo):
                results.append({"repo": repo, "skipped": "invalid_name"})
                continue
            try:
                r = await c.get(
                    f"https://api.github.com/repos/{repo}/commits",
                    params={"per_page": 1},
                )
            except Exception as e:
                results.append({"repo": repo, "error": repr(e)})
                continue
            if r.status_code != 200:
                results.append({"repo": repo, "http_status": r.status_code})
                continue
            try:
                data = r.json()
            except Exception:
                data = []
            if not isinstance(data, list) or not data:
                results.append({"repo": repo, "commits": 0})
                continue
            head = data[0]
            sha = head.get("sha", "")
            prev = _LAST_SEEN_COMMIT.get(repo)
            entry = {
                "repo": repo, "head": sha[:12],
                "message": (head.get("commit") or {}).get("message", "")
                              .splitlines()[0:1][0:1],
                "author": ((head.get("commit") or {}).get("author") or {})
                              .get("name", ""),
            }
            if prev and prev != sha:
                entry["new_commit"] = True
                await _telegram(
                    f"GitHub: {repo} new HEAD {sha[:12]} by "
                    f"{entry['author']}: "
                    f"{(head.get('commit') or {}).get('message','')[:200]}"
                )
                await _emit_episode(
                    "kairos", "github.poll",
                    input_=repo, output=f"new_head={sha[:12]}",
                    metadata={"repo": repo, "sha": sha, "prev": prev},
                )
            _LAST_SEEN_COMMIT[repo] = sha
            log.info("kairos.github.head", extra={
                "repo": repo, "sha": sha[:12], "new": prev != sha,
            })
            results.append(entry)
    return {"repos": results, "count": len(results)}


# ─── 3) Health check ──────────────────────────────────────────────────────


def _disk_usage(path: str = "/") -> dict[str, Any]:
    try:
        total, used, free = shutil.disk_usage(path)
        pct = (used / total * 100.0) if total else 0.0
        return {"path": path, "total": total, "used": used,
                "free": free, "pct": round(pct, 1)}
    except Exception as e:
        return {"path": path, "error": repr(e)}


def _mem_info() -> dict[str, Any]:
    """Parse /proc/meminfo. Returns pct used and totals in kB."""
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split(":", 1)
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                val = parts[1].strip().split()[0]
                try:
                    info[key] = int(val)
                except ValueError:
                    continue
    except FileNotFoundError:
        return {"available": False}
    total = info.get("MemTotal", 0)
    free = info.get("MemAvailable", info.get("MemFree", 0))
    pct = ((total - free) / total * 100.0) if total else 0.0
    return {"available": True, "total_kb": total, "available_kb": free,
            "pct": round(pct, 1)}


def _cpu_load() -> dict[str, Any]:
    try:
        load1, load5, load15 = os.getloadavg()
    except (AttributeError, OSError) as e:
        return {"available": False, "error": repr(e)}
    cores = os.cpu_count() or 1
    pct = round(load1 / cores * 100.0, 1)
    return {"available": True, "cores": cores,
            "load1": load1, "load5": load5, "load15": load15,
            "pct": pct}


async def _health_check() -> dict[str, Any]:
    disk = _disk_usage("/")
    mem = _mem_info()
    cpu = _cpu_load()
    alerts: list[str] = []
    if isinstance(disk.get("pct"), float) and \
       disk["pct"] >= _int_env("KAIROS_HEALTH_DISK_PCT",
                               DEFAULT_HEALTH_DISK_PCT):
        alerts.append(f"disk {disk['pct']}% used on /")
    if mem.get("available") and mem.get("pct", 0) >= \
       _int_env("KAIROS_HEALTH_MEM_PCT", DEFAULT_HEALTH_MEM_PCT):
        alerts.append(f"memory {mem['pct']}% used")
    if cpu.get("available") and cpu.get("pct", 0) >= \
       _int_env("KAIROS_HEALTH_CPU_PCT", DEFAULT_HEALTH_CPU_PCT):
        alerts.append(f"cpu load1 {cpu['pct']}% of {cpu['cores']} cores")
    if alerts:
        await _telegram("KAIROS health alert: " + "; ".join(alerts))
        await _emit_episode("kairos", "health.alert",
                            input_=";".join(alerts),
                            metadata={"disk": disk, "mem": mem, "cpu": cpu})
    return {"disk": disk, "mem": mem, "cpu": cpu, "alerts": alerts}


# ─── 4) autoDream ─────────────────────────────────────────────────────────


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{3,}")
_STOPWORDS = {
    "this", "that", "with", "from", "into", "your", "have", "been",
    "they", "them", "their", "there", "which", "would", "could", "should",
    "about", "where", "when", "while", "what", "will", "shall", "than",
    "then", "these", "those", "some", "such", "also", "only", "even",
    "more", "most", "many", "much", "true", "false", "none", "null",
    "user", "system", "input", "output",
}


def _embed_stub(text: str, dim: int = 384) -> list[float]:
    """Deterministic, dependency-free 'embedding'.

    Hashes overlapping char-trigrams into `dim` buckets, then L2-normalises.
    Used as fallback when real embedding fails.
    """
    import hashlib
    import math

    buckets = [0.0] * dim
    s = text.lower()
    if not s:
        return buckets
    for i in range(len(s) - 2):
        tri = s[i:i + 3]
        h = int.from_bytes(hashlib.sha1(tri.encode()).digest()[:8], "big")
        idx = h % dim
        sign = 1.0 if (h >> 63) & 1 else -1.0
        buckets[idx] += sign
    norm = math.sqrt(sum(b * b for b in buckets))
    if norm > 0:
        buckets = [b / norm for b in buckets]
    return buckets


async def _embed(text: str) -> list[float]:
    """Get embedding for text. Uses real embeddings, falls back to stub."""
    try:
        from core.embeddings import embed_text
        return await embed_text(text)
    except Exception as e:
        log = _get_logger()
        log.warning(f"[kairos._embed] Real embedding failed: {e}, using stub")
        return _embed_stub(text)


def extract_patterns(
    episodes: list[dict[str, Any]],
    *,
    min_count: int = 3,
) -> list[dict[str, Any]]:
    """Return aggregated patterns from a batch of episodes.

    For now: token frequency by actor+tool combination, plus a count of
    successes vs failures per skill. Cheap, deterministic, no model.
    """
    pair_tokens: dict[tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
    outcomes: dict[str, dict[str, int]] = collections.defaultdict(
        lambda: {"ok": 0, "fail": 0, "total": 0})
    for ep in episodes:
        actor = ep.get("actor") or ""
        tool = ep.get("tool") or ""
        if actor and tool:
            text = " ".join(str(ep.get(k, "") or "") for k in
                            ("input", "output", "failure_mode"))
            for tok in _WORD_RE.findall(text.lower()):
                if tok in _STOPWORDS:
                    continue
                pair_tokens[(actor, tool)][tok] += 1
        bucket = outcomes[tool or "unknown"]
        bucket["total"] += 1
        if ep.get("exit_code") == 0 and (ep.get("failure_mode") in (None, "")):
            bucket["ok"] += 1
        else:
            bucket["fail"] += 1

    patterns: list[dict[str, Any]] = []
    for (actor, tool), tokens in pair_tokens.items():
        for word, count in tokens.most_common(5):
            if count < min_count:
                continue
            patterns.append({
                "kind": "co-occurrence",
                "actor": actor, "tool": tool,
                "term": word, "count": count,
            })

    for tool, b in outcomes.items():
        if b["total"] < min_count:
            continue
        rate = b["fail"] / b["total"] if b["total"] else 0.0
        patterns.append({
            "kind": "outcome-rate",
            "tool": tool,
            "total": b["total"],
            "ok": b["ok"], "fail": b["fail"],
            "fail_rate": round(rate, 3),
        })
    return patterns


async def auto_dream(
    *,
    lookback_hours: Optional[int] = None,
    max_episodes: Optional[int] = None,
) -> dict[str, Any]:
    """Scan recent episodes, extract patterns, upsert into semantic memory.

    Returns a structured summary: how many episodes, patterns, writes.
    Safe to call any time; honors KAIROS_DRY_RUN.
    """
    lookback_h = lookback_hours or _int_env(
        "DREAM_LOOKBACK_HOURS", DEFAULT_DREAM_LOOKBACK_HOURS)
    cap = max_episodes or _int_env(
        "DREAM_MAX_EPISODES", DEFAULT_DREAM_MAX_EPISODES)
    log = _get_logger()

    summary: dict[str, Any] = {
        "lookback_hours": lookback_h, "max_episodes": cap,
        "episodes_scanned": 0, "patterns": 0, "writes": 0, "errors": [],
    }
    try:
        from . import memory
    except Exception as e:
        summary["errors"].append(f"memory_import_failed: {e!r}")
        return summary

    try:
        since = datetime.now(timezone.utc) - timedelta(hours=lookback_h)
        episodes = await memory.recent_episodes(limit=cap, since=since)
        summary["episodes_scanned"] = len(episodes)
    except Exception as e:
        summary["errors"].append(f"fetch_failed: {e!r}")
        return summary

    patterns = extract_patterns(episodes)
    summary["patterns"] = len(patterns)
    log.info("kairos.dream.patterns", extra={
        "count": len(patterns), "episodes": len(episodes),
        "lookback_h": lookback_h,
    })

    if _bool_env("KAIROS_DRY_RUN", False):
        summary["dry_run"] = True
        return summary

    for p in patterns:
        try:
            subject = (p.get("tool") or p.get("term") or "pattern")[:128]
            content = " ".join(f"{k}={v}" for k, v in p.items() if k != "kind")
            confidence = 0.5
            if p["kind"] == "outcome-rate":
                # higher confidence with more samples
                confidence = min(0.95, 0.4 + p["total"] / 200.0)
            await memory.upsert_semantic(
                kind=p["kind"],
                subject=subject,
                content=content,
                embedding=await _embed(content),
                confidence=confidence,
                metadata=p,
            )
            summary["writes"] += 1
        except Exception as e:
            summary["errors"].append(f"upsert_failed: {e!r}")
    await _emit_episode("kairos", "dream",
                        output=f"patterns={summary['patterns']} writes={summary['writes']}",
                        metadata=summary)
    return summary


# ─── 5) Idle detection ────────────────────────────────────────────────────


async def _seconds_since_last_episode() -> Optional[float]:
    """How long since the last user/system episode landed."""
    try:
        from . import memory
        rows = await memory.recent_episodes(limit=1)
        if not rows:
            return None
        ts = rows[0].get("ts")
        if isinstance(ts, datetime):
            now = datetime.now(timezone.utc) if ts.tzinfo else datetime.utcnow()
            return (now - ts).total_seconds()
        return None
    except Exception:
        return None


# ─── KAIROS daemon ────────────────────────────────────────────────────────


@dataclasses.dataclass
class KairosConfig:
    interval_s: int = dataclasses.field(default_factory=lambda: _int_env("KAIROS_INTERVAL", DEFAULT_INTERVAL_S))
    task_batch: int = dataclasses.field(default_factory=lambda: _int_env("KAIROS_TASK_BATCH", DEFAULT_TASK_BATCH))
    github_every_n: int = dataclasses.field(default_factory=lambda: _int_env("KAIROS_GITHUB_POLL_EVERY_N", DEFAULT_GITHUB_POLL_EVERY_N))
    idle_threshold_s: int = dataclasses.field(default_factory=lambda: _int_env("DREAM_IDLE_THRESHOLD", DEFAULT_DREAM_IDLE_S))
    dry_run: bool = dataclasses.field(default_factory=lambda: _bool_env("KAIROS_DRY_RUN", False))


class Kairos:
    """KAIROS background scheduler.

        kairos = Kairos()
        await kairos.start()    # blocks forever
        # or
        task = asyncio.create_task(kairos.start())
        ...
        await kairos.stop()
    """

    def __init__(self, config: Optional[KairosConfig] = None) -> None:
        self.cfg = config or KairosConfig()
        self.log = _get_logger()
        self._stop = asyncio.Event()
        self._ticks = 0
        self._last_dream_at: Optional[float] = None

    async def start(self) -> None:
        self.log.info("kairos.start", extra=dataclasses.asdict(self.cfg))
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    summary = await self.tick()
                    self.log.info("kairos.tick", extra=summary)
                except Exception as e:  # pragma: no cover
                    self.log.exception("kairos.tick.exception",
                                       extra={"exc": repr(e)})
                # Sleep until the next interval, honoring stop.
                elapsed = time.monotonic() - t0
                wait = max(1.0, self.cfg.interval_s - elapsed)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.log.info("kairos.stopped", extra={"ticks": self._ticks})

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> dict[str, Any]:
        self._ticks += 1
        out: dict[str, Any] = {"tick": self._ticks}

        # 1) Task queue drain
        out["tasks"] = await _drain_task_queue(self.cfg.task_batch)

        # 2) GitHub poll (only every N ticks to save rate limit)
        if self._ticks % max(1, self.cfg.github_every_n) == 0:
            out["github"] = await _poll_github()

        # 3) Health
        out["health"] = await _health_check()

        # 4) autoDream if idle long enough
        idle = await _seconds_since_last_episode()
        out["idle_s"] = idle
        if idle is not None and idle >= self.cfg.idle_threshold_s:
            now = time.monotonic()
            # Don't dream more than once per idle_threshold window.
            if (self._last_dream_at is None or
                    now - self._last_dream_at >= self.cfg.idle_threshold_s):
                out["dream"] = await auto_dream()
                self._last_dream_at = now

        return out


# ─── CLI ──────────────────────────────────────────────────────────────────


def main() -> None:  # pragma: no cover
    import argparse
    p = argparse.ArgumentParser(prog="core.kairos")
    p.add_argument("--once", action="store_true",
                   help="run a single tick and exit")
    p.add_argument("--dream", action="store_true",
                   help="run auto_dream() once and exit")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.dry_run:
        os.environ["KAIROS_DRY_RUN"] = "true"

    async def _run():
        if args.dream:
            print(await auto_dream())
            return
        k = Kairos()
        if args.once:
            print(await k.tick())
            return
        try:
            await k.start()
        except KeyboardInterrupt:
            await k.stop()

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    main()
