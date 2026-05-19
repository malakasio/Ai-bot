"""Red-Zone Sentinel — autonomous host-defense daemon.

Watches /var/log/auth.log for SSH brute-force activity and the integrity of
critical /etc files. On a trigger event it blocks the attacker via iptables
and ufw, snapshots /etc into a git repo, restarts JARVIS services, and
emits a Telegram alert.

Design notes
------------
* Pure asyncio. Two long-running tasks (log tail + integrity poller) plus
  ad-hoc lockdown actions; everything coordinates through one shared lock
  so two simultaneous triggers don't both restart the same service.
* Defensive: every external call (subprocess, file read, HTTP) is wrapped;
  failures are logged but never crash the daemon. The daemon retreating
  silently is worse than the daemon limping.
* Safe by default: lockdown actions can be disabled (dry-run) via
  JARVIS_SENTINEL_DRY_RUN=true. CI and unit tests run with dry-run on.
* Privileged: iptables/ufw/systemctl/git on /etc require root. The
  daemon refuses to take destructive action if it's not running as root
  and dry-run is off.

Environment
-----------
  JARVIS_SENTINEL_AUTH_LOG       default /var/log/auth.log
  JARVIS_SENTINEL_INTEGRITY_PATHS comma-list, default /etc/passwd,/etc/ssh/sshd_config
  JARVIS_SENTINEL_INTEGRITY_INTERVAL_S  default 5
  JARVIS_SENTINEL_FAIL_THRESHOLD default 5  (failures from one IP -> trigger)
  JARVIS_SENTINEL_FAIL_WINDOW_S  default 60
  JARVIS_SENTINEL_WHITELIST      comma-list of IPs/CIDRs never to block
  JARVIS_SENTINEL_SERVICES       comma-list, default "jarvis"
  JARVIS_SENTINEL_GIT_DIR        default /var/lib/jarvis/etc-snapshots
  JARVIS_SENTINEL_DRY_RUN        default false
  TELEGRAM_BOT_TOKEN / TELEGRAM_USER_ID
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ─── Public constants ─────────────────────────────────────────────────────

DEFAULT_AUTH_LOG = "/var/log/auth.log"
DEFAULT_INTEGRITY_PATHS = ("/etc/passwd", "/etc/ssh/sshd_config")
DEFAULT_INTEGRITY_INTERVAL_S = 5
DEFAULT_FAIL_THRESHOLD = 5
DEFAULT_FAIL_WINDOW_S = 60
DEFAULT_SERVICES = ("jarvis",)
DEFAULT_GIT_DIR = "/var/lib/jarvis/etc-snapshots"

FAILED_PW_RE = re.compile(
    r"Failed password for (?:invalid user )?(?P<user>\S+) "
    r"from (?P<ip>\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]+) port \d+",
)


# ─── Logger ───────────────────────────────────────────────────────────────


def _get_logger():
    # Reuse the agent's structured logger if it's available; fall back to a
    # minimal stderr logger otherwise.
    try:
        from . import agent as _agent
        return _agent.get_logger()
    except Exception:
        import logging
        log = logging.getLogger("jarvis.sentinel")
        if not log.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            ))
            log.addHandler(h)
            log.setLevel(logging.INFO)
        return log


# ─── Helpers ──────────────────────────────────────────────────────────────


def _bool_env(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _list_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts or default


def _int_env(name: str, default: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v) if v is not None and v != "" else default
    except ValueError:
        return default


def _is_ip(addr: str) -> bool:
    try:
        ipaddress.ip_address(addr)
        return True
    except ValueError:
        return False


def _ip_in_whitelist(addr: str, whitelist: tuple[str, ...]) -> bool:
    if not whitelist:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    for entry in whitelist:
        try:
            if ip == ipaddress.ip_address(entry):
                return True
        except ValueError:
            pass
        try:
            if ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            pass
    return False


def _sha256_file(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except (FileNotFoundError, PermissionError, IsADirectoryError, OSError):
        return None


# ─── Config dataclass ─────────────────────────────────────────────────────


@dataclass
class SentinelConfig:
    auth_log: Path = field(
        default_factory=lambda: Path(os.environ.get(
            "JARVIS_SENTINEL_AUTH_LOG", DEFAULT_AUTH_LOG))
    )
    integrity_paths: tuple[Path, ...] = field(
        default_factory=lambda: tuple(
            Path(p) for p in _list_env(
                "JARVIS_SENTINEL_INTEGRITY_PATHS", DEFAULT_INTEGRITY_PATHS)
        )
    )
    integrity_interval_s: int = field(
        default_factory=lambda: _int_env(
            "JARVIS_SENTINEL_INTEGRITY_INTERVAL_S",
            DEFAULT_INTEGRITY_INTERVAL_S)
    )
    fail_threshold: int = field(
        default_factory=lambda: _int_env(
            "JARVIS_SENTINEL_FAIL_THRESHOLD", DEFAULT_FAIL_THRESHOLD)
    )
    fail_window_s: int = field(
        default_factory=lambda: _int_env(
            "JARVIS_SENTINEL_FAIL_WINDOW_S", DEFAULT_FAIL_WINDOW_S)
    )
    whitelist: tuple[str, ...] = field(
        default_factory=lambda: _list_env("JARVIS_SENTINEL_WHITELIST", ())
    )
    services: tuple[str, ...] = field(
        default_factory=lambda: _list_env(
            "JARVIS_SENTINEL_SERVICES", DEFAULT_SERVICES)
    )
    git_dir: Path = field(
        default_factory=lambda: Path(os.environ.get(
            "JARVIS_SENTINEL_GIT_DIR", DEFAULT_GIT_DIR))
    )
    dry_run: bool = field(
        default_factory=lambda: _bool_env("JARVIS_SENTINEL_DRY_RUN", False)
    )


# ─── Subprocess wrapper ──────────────────────────────────────────────────


async def _run(cmd: list[str], *, timeout: float = 30.0
               ) -> tuple[int, str, str]:
    """Run a command; never raise. Returns (rc, stdout, stderr)."""
    log = _get_logger()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        log.warning("sentinel.cmd.missing", extra={"cmd": cmd, "exc": repr(e)})
        return 127, "", str(e)
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        log.error("sentinel.cmd.timeout", extra={"cmd": cmd})
        return 124, "", "timeout"
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


async def _send_telegram(text: str) -> bool:
    """Reuse agent.send_telegram_alert if available; else inline."""
    log = _get_logger()
    try:
        from . import agent as _agent
        return await _agent.send_telegram_alert(text)
    except Exception:
        pass
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (os.environ.get("TELEGRAM_USER_ID")
               or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        log.warning("sentinel.telegram.skip", extra={"reason": "missing_creds"})
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as c:
            resp = await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text,
                      "disable_web_page_preview": True},
            )
        return resp.status_code == 200
    except Exception as e:
        log.error("sentinel.telegram.fail", extra={"exc": repr(e)})
        return False


# ─── The sentinel ────────────────────────────────────────────────────────


@dataclass
class _Hit:
    ip: str
    user: str
    ts: float


class RedZoneSentinel:
    """Autonomous defense daemon. Use as:

        sentinel = RedZoneSentinel()
        await sentinel.start()          # blocks forever
        # or
        task = asyncio.create_task(sentinel.start())
        ...
        await sentinel.stop()
    """

    def __init__(self, config: Optional[SentinelConfig] = None) -> None:
        self.cfg = config or SentinelConfig()
        self.log = _get_logger()
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()  # serializes lockdown sequences
        self._tasks: list[asyncio.Task] = []
        self._failures: dict[str, list[float]] = {}
        self._blocked: set[str] = set()
        self._hashes: dict[Path, Optional[str]] = {}
        self.triggered_count = 0    # observability hook

    # ── Public API ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch both async tasks and wait until stop() is called."""
        self.log.info("sentinel.start", extra={
            "auth_log": str(self.cfg.auth_log),
            "integrity_paths": [str(p) for p in self.cfg.integrity_paths],
            "fail_threshold": self.cfg.fail_threshold,
            "fail_window_s": self.cfg.fail_window_s,
            "dry_run": self.cfg.dry_run,
            "services": list(self.cfg.services),
        })
        self._tasks = [
            asyncio.create_task(self.monitor_ssh_logs(),
                                name="sentinel.ssh"),
            asyncio.create_task(self.check_file_integrity(),
                                name="sentinel.integrity"),
        ]
        try:
            await self._stop_event.wait()
        finally:
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self.log.info("sentinel.stopped",
                          extra={"triggered_count": self.triggered_count})

    async def stop(self) -> None:
        self._stop_event.set()

    # ── 1) SSH log tail ───────────────────────────────────────────────

    async def monitor_ssh_logs(self) -> None:
        """Tail auth.log, count failed-password attempts per IP, fire on threshold."""
        log = self.log
        path = self.cfg.auth_log

        # Wait for the log to appear (e.g. on a fresh box) instead of
        # crashing the task.
        backoff = 1.0
        while not self._stop_event.is_set():
            if path.exists():
                break
            log.warning("sentinel.ssh.waiting", extra={"path": str(path),
                                                        "backoff_s": backoff})
            try:
                await asyncio.wait_for(self._stop_event.wait(),
                                       timeout=backoff)
                return
            except asyncio.TimeoutError:
                backoff = min(backoff * 2, 30.0)

        try:
            # Open and seek to the end — we only care about new lines.
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, 2)
                while not self._stop_event.is_set():
                    line = f.readline()
                    if not line:
                        await asyncio.sleep(0.5)
                        continue
                    await self._handle_auth_line(line)
        except PermissionError as e:
            log.error("sentinel.ssh.permission", extra={
                "path": str(path), "exc": repr(e),
            })
            return
        except FileNotFoundError as e:
            log.error("sentinel.ssh.gone", extra={
                "path": str(path), "exc": repr(e),
            })
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("sentinel.ssh.error", extra={"exc": repr(e)})

    async def _handle_auth_line(self, line: str) -> None:
        m = FAILED_PW_RE.search(line)
        if not m:
            return
        ip = m.group("ip")
        user = m.group("user")
        if not _is_ip(ip):
            return
        if _ip_in_whitelist(ip, self.cfg.whitelist):
            self.log.info("sentinel.ssh.whitelisted",
                          extra={"ip": ip, "user": user})
            return
        if ip in self._blocked:
            return

        now = time.time()
        bucket = self._failures.setdefault(ip, [])
        bucket.append(now)
        # prune outside the window
        cutoff = now - self.cfg.fail_window_s
        self._failures[ip] = [t for t in bucket if t >= cutoff]
        n = len(self._failures[ip])

        self.log.info("sentinel.ssh.fail", extra={
            "ip": ip, "user": user, "count": n,
            "threshold": self.cfg.fail_threshold,
        })

        if n >= self.cfg.fail_threshold:
            self._blocked.add(ip)
            self.triggered_count += 1
            await self.trigger_lockdown(
                ip=ip,
                reason=f"SSH brute force: {n} failed attempts in "
                       f"{self.cfg.fail_window_s}s, last user={user}",
                evidence=line.strip(),
            )

    # ── 2) File integrity ─────────────────────────────────────────────

    async def check_file_integrity(self) -> None:
        log = self.log
        # Prime the baseline.
        for p in self.cfg.integrity_paths:
            self._hashes[p] = _sha256_file(p)
            log.info("sentinel.integrity.baseline", extra={
                "path": str(p),
                "sha256": self._hashes[p] or "<missing>",
            })
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.cfg.integrity_interval_s,
                    )
                    return  # stop_event set
                except asyncio.TimeoutError:
                    pass
                for p in self.cfg.integrity_paths:
                    current = _sha256_file(p)
                    prev = self._hashes.get(p)
                    if current is None and prev is None:
                        continue
                    if current != prev:
                        log.warning("sentinel.integrity.changed", extra={
                            "path": str(p),
                            "before": prev,
                            "after": current,
                        })
                        self._hashes[p] = current
                        self.triggered_count += 1
                        await self.trigger_lockdown(
                            ip=None,
                            reason=f"file integrity change on {p}",
                            evidence=f"sha256 {prev!r} -> {current!r}",
                        )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("sentinel.integrity.error", extra={"exc": repr(e)})

    # ── 3) Lockdown sequence ──────────────────────────────────────────

    async def trigger_lockdown(self, *, ip: Optional[str], reason: str,
                                evidence: str = "") -> dict:
        """Run the full lockdown sequence. Returns a structured report.

        Sequence:
          1. iptables -A INPUT -s <ip> -j DROP   (if ip given)
             ufw insert 1 deny from <ip>          (if ip given)
          2. git -C <git_dir> add -A && git commit -m "..."
          3. systemctl restart <service> for each configured service
          4. Telegram notification

        All steps are best-effort; one failing does not abort the rest.
        """
        async with self._lock:
            report: dict = {
                "ts": int(time.time()),
                "ip": ip,
                "reason": reason,
                "evidence": evidence,
                "dry_run": self.cfg.dry_run,
                "steps": {},
            }
            self.log.warning("sentinel.lockdown.start", extra=report)

            report["steps"]["block_ip"] = await self._block_ip(ip)
            report["steps"]["snapshot_etc"] = await self._snapshot_etc(reason)
            report["steps"]["restart_services"] = await self._restart_services()
            report["steps"]["telegram"] = await self._notify(report)

            self.log.warning("sentinel.lockdown.done", extra=report)
            return report

    # ── Lockdown primitives ───────────────────────────────────────────

    async def _block_ip(self, ip: Optional[str]) -> dict:
        out: dict = {"ip": ip, "iptables": None, "ufw": None}
        if not ip:
            out["skipped"] = "no_ip"
            return out
        if not _is_ip(ip):
            out["skipped"] = "invalid_ip"
            return out
        if self.cfg.dry_run:
            out["skipped"] = "dry_run"
            return out
        if os.geteuid() != 0:
            out["skipped"] = "not_root"
            return out

        # iptables
        ipt = shutil.which("iptables")
        if ipt:
            rc, so, se = await _run(
                [ipt, "-A", "INPUT", "-s", ip, "-j", "DROP"])
            out["iptables"] = {"rc": rc, "err": se.strip()[:512]}
        else:
            out["iptables"] = {"skipped": "missing_binary"}

        # ufw
        ufw = shutil.which("ufw")
        if ufw:
            rc, so, se = await _run(
                [ufw, "insert", "1", "deny", "from", ip])
            out["ufw"] = {"rc": rc, "err": se.strip()[:512]}
        else:
            out["ufw"] = {"skipped": "missing_binary"}

        return out

    async def _snapshot_etc(self, reason: str) -> dict:
        out: dict = {"dir": str(self.cfg.git_dir)}
        if self.cfg.dry_run:
            out["skipped"] = "dry_run"
            return out
        if os.geteuid() != 0:
            out["skipped"] = "not_root"
            return out
        git = shutil.which("git")
        if not git:
            out["skipped"] = "missing_binary"
            return out
        try:
            self.cfg.git_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            out["skipped"] = f"mkdir_failed: {e}"
            return out

        # Init the repo lazily so the daemon can boot on a fresh host.
        if not (self.cfg.git_dir / ".git").exists():
            rc, _, se = await _run([git, "-C", str(self.cfg.git_dir), "init"])
            out["init"] = {"rc": rc, "err": se.strip()[:256]}

        # Mirror /etc into the snapshot dir. rsync if available; cp otherwise.
        rsync = shutil.which("rsync")
        if rsync:
            rc, _, se = await _run([
                rsync, "-a", "--delete",
                "--exclude=/.git", "--exclude=/.gitignore",
                "/etc/", f"{self.cfg.git_dir}/etc/",
            ], timeout=120)
            out["copy"] = {"tool": "rsync", "rc": rc, "err": se.strip()[:256]}
        else:
            cp = shutil.which("cp")
            if cp:
                rc, _, se = await _run([
                    cp, "-a", "/etc/.", f"{self.cfg.git_dir}/etc/",
                ], timeout=120)
                out["copy"] = {"tool": "cp", "rc": rc, "err": se.strip()[:256]}
            else:
                out["copy"] = {"skipped": "missing_binary"}
                return out

        rc, _, se = await _run([git, "-C", str(self.cfg.git_dir),
                                "add", "-A"])
        out["add"] = {"rc": rc, "err": se.strip()[:256]}
        msg = (f"snapshot: {reason} "
               f"({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})")
        rc, _, se = await _run([
            git, "-C", str(self.cfg.git_dir),
            "-c", "user.name=jarvis-sentinel",
            "-c", "user.email=sentinel@jarvis.local",
            "commit", "-m", msg, "--allow-empty",
        ])
        out["commit"] = {"rc": rc, "err": se.strip()[:256], "msg": msg}
        return out

    async def _restart_services(self) -> dict:
        out: dict = {"services": list(self.cfg.services), "results": []}
        if self.cfg.dry_run:
            out["skipped"] = "dry_run"
            return out
        if os.geteuid() != 0:
            out["skipped"] = "not_root"
            return out
        sctl = shutil.which("systemctl")
        if not sctl:
            out["skipped"] = "missing_binary"
            return out
        for svc in self.cfg.services:
            rc, _, se = await _run([sctl, "restart", svc], timeout=60)
            out["results"].append({"svc": svc, "rc": rc,
                                    "err": se.strip()[:256]})
        return out

    async def _notify(self, report: dict) -> dict:
        def _step_summary(k: str, v: object) -> str:
            if not isinstance(v, dict):
                return f"{k}=?"
            if "skipped" in v:
                return f"{k}=skip:{v['skipped']}"
            return f"{k}=ok"

        steps_summary = ", ".join(
            _step_summary(k, v)
            for k, v in report["steps"].items()
            if k != "telegram"
        )
        lines = [
            "JARVIS Sentinel - lockdown triggered",
            f"reason: {report['reason']}",
            f"ip: {report.get('ip') or '-'}",
            f"dry_run: {report['dry_run']}",
            f"evidence: {report.get('evidence','')[:300]}",
            f"steps: {steps_summary}",
        ]
        text = "\n".join(lines)
        ok = await _send_telegram(text)
        return {"sent": ok, "preview": text[:200]}


# ─── Module entry / CLI ──────────────────────────────────────────────────


def main() -> None:  # pragma: no cover - CLI shim
    import argparse
    parser = argparse.ArgumentParser(prog="core.sentinel")
    parser.add_argument("--dry-run", action="store_true",
                        help="force dry-run (no privileged actions)")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["JARVIS_SENTINEL_DRY_RUN"] = "true"
    sentinel = RedZoneSentinel()
    try:
        asyncio.run(sentinel.start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
