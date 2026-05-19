#!/usr/bin/env python3
"""scripts/check_deps.py — JARVIS v7.0 dependency inventory.

Imports every Python module JARVIS uses, plus probes for the system
binaries it shells out to, and prints a capability table. Exits 0 unless
``--strict`` was passed and at least one required dependency is missing.

Usage:
  python3 scripts/check_deps.py                   # print table, exit 0
  python3 scripts/check_deps.py --strict          # exit 1 on any missing
  python3 scripts/check_deps.py --json            # machine-readable
  python3 scripts/check_deps.py --capability voice  # show only one group
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Dep:
    name: str
    kind: str          # "python" | "bin" | "env"
    capability: str
    required: bool = False
    pip: Optional[str] = None
    note: str = ""

    # Filled in by check()
    present: bool = False
    detail: str = ""


def _check_python(name: str) -> tuple[bool, str]:
    try:
        mod = importlib.import_module(name)
    except Exception as e:
        return False, repr(e)
    ver = getattr(mod, "__version__", "")
    return True, f"v{ver}" if ver else "ok"


def _check_bin(name: str) -> tuple[bool, str]:
    # Synthetic check for the Python interpreter version.
    if name == "python3.10+":
        if sys.version_info >= (3, 10):
            return True, f"v{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        return False, f"have v{sys.version_info.major}.{sys.version_info.minor} (need >=3.10)"
    path = shutil.which(name)
    if path:
        return True, path
    return False, "not found"


def _check_env(name: str) -> tuple[bool, str]:
    import os
    val = os.environ.get(name, "")
    if val:
        return True, f"set ({len(val)} chars)"
    return False, "unset"


def collect() -> list[Dep]:
    deps: list[Dep] = [
        # Core / runtime
        Dep("python3.10+", "bin", "core", required=True),
        Dep("git", "bin", "core", required=True),
        Dep("asyncpg", "python", "core/memory", required=True,
            pip="asyncpg",
            note="async PostgreSQL driver"),
        Dep("anthropic", "python", "core/agent", required=True,
            pip="anthropic",
            note="Claude API client"),
        Dep("httpx", "python", "core/agent", required=True,
            pip="httpx",
            note="async HTTP, used by net + telegram + mcp"),
        Dep("tenacity", "python", "core/agent", required=False,
            pip="tenacity",
            note="retry decorator; hand-rolled fallback exists"),

        # Postgres / vector
        Dep("psql", "bin", "core/memory", required=True,
            note="schema bootstrap via scripts/init_db.sql"),
        Dep("pgvector", "python", "core/memory", required=False,
            pip="pgvector",
            note="binds the vector type for asyncpg"),

        # MCP / filesystem
        Dep("rsync", "bin", "sentinel/snapshot", required=False,
            note="preferred /etc snapshot tool; falls back to cp -a"),

        # Network MCP
        Dep("nmap", "bin", "mcp/network (lab only)", required=False),
        Dep("ping", "bin", "mcp/network", required=False),
        Dep("iptables", "bin", "core/sentinel (lab only)", required=False),
        Dep("ufw", "bin", "core/sentinel (lab only)", required=False),

        # Voice
        Dep("fastapi", "python", "voice", required=False,
            pip="fastapi",
            note="WebSocket app factory"),
        Dep("uvicorn", "python", "voice", required=False,
            pip="uvicorn",
            note="ASGI runner"),
        Dep("websockets", "python", "voice", required=False,
            pip="websockets",
            note="Deepgram + ElevenLabs streaming"),

        # Daemons
        Dep("systemctl", "bin", "kairos/rollback", required=False,
            note="service restart hook"),

        # Credentials (env)
        Dep("ANTHROPIC_API_KEY", "env", "agent/llm", required=False,
            note="or systemd LoadCredential"),
        Dep("DEEPGRAM_API_KEY", "env", "voice", required=False),
        Dep("ELEVENLABS_API_KEY", "env", "voice", required=False),
        Dep("TELEGRAM_BOT_TOKEN", "env", "kairos/sentinel", required=False),
        Dep("APIFY_TOKEN", "env", "mcp/automation", required=False),
        Dep("N8N_WEBHOOK_BASE", "env", "mcp/automation", required=False),
        Dep("DATABASE_URL", "env", "core/database", required=False,
            note="or POSTGRES_HOST + POSTGRES_USER + ..."),
    ]

    for d in deps:
        if d.kind == "python":
            ok, detail = _check_python(d.name)
        elif d.kind == "bin":
            ok, detail = _check_bin(d.name)
        elif d.kind == "env":
            ok, detail = _check_env(d.name)
        else:
            ok, detail = False, "unknown kind"
        d.present, d.detail = ok, detail

    return deps


def render_table(deps: list[Dep], capability_filter: Optional[str] = None
                ) -> str:
    rows = [d for d in deps
            if not capability_filter or d.capability == capability_filter]

    # Group by capability for readability.
    groups: dict[str, list[Dep]] = {}
    for d in rows:
        groups.setdefault(d.capability, []).append(d)

    out = []
    out.append(
        f"{'NAME':<22} {'KIND':<7} {'STATUS':<10} {'REQ':<4} DETAIL"
    )
    out.append("-" * 78)
    for cap, gd in sorted(groups.items()):
        out.append(f"\n[{cap}]")
        for d in gd:
            status = "ok" if d.present else "MISSING"
            req = "yes" if d.required else "no"
            detail = d.detail
            if d.pip and not d.present and d.kind == "python":
                detail += f"  (pip install {d.pip})"
            out.append(f"{d.name:<22} {d.kind:<7} {status:<10} {req:<4} {detail}")
            if d.note and not d.present:
                out.append(f"{'':<46}note: {d.note}")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="check_deps")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 if any required dep is missing")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of the human table")
    parser.add_argument("--capability", default=None,
                        help="filter table to one capability group")
    args = parser.parse_args(argv)

    deps = collect()
    missing_required = [d for d in deps if d.required and not d.present]

    if args.json:
        payload = {
            "deps": [
                {"name": d.name, "kind": d.kind, "capability": d.capability,
                 "required": d.required, "present": d.present,
                 "detail": d.detail, "pip": d.pip, "note": d.note}
                for d in deps
            ],
            "missing_required": [d.name for d in missing_required],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(render_table(deps, args.capability))
        print()
        if missing_required:
            print(f"REQUIRED MISSING: {', '.join(d.name for d in missing_required)}")
        else:
            print("all required deps present")

    if args.strict and missing_required:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
