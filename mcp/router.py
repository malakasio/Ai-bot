"""MCP router.

Loads `config/mcp_config.json`, imports the enabled servers, and dispatches
tool calls to the right server. Tools are identified either as
``"<server>.<tool>"`` or by their bare ``<tool>`` name when unambiguous.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ._common import Server, err


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "mcp_config.json"


@dataclass
class Router:
    config_path: Path = DEFAULT_CONFIG
    servers: dict[str, Server] = field(default_factory=dict)
    _tool_index: dict[str, str] = field(default_factory=dict)  # bare name -> server

    def load(self) -> "Router":
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        servers_cfg = cfg.get("servers", [])
        for entry in servers_cfg:
            if not entry.get("enabled", True):
                continue
            name = entry["name"]
            module_path = entry["module"]  # e.g. "mcp.filesystem_mcp"
            mod = importlib.import_module(module_path)
            if not hasattr(mod, "get_server"):
                raise RuntimeError(f"{module_path} has no get_server()")
            srv = mod.get_server()
            if not isinstance(srv, Server):
                raise RuntimeError(f"{module_path}.get_server() did not return Server")
            self.servers[name] = srv
            for tool_name in srv.tools:
                # Bare tool name is unique only if a single server claims it.
                if tool_name in self._tool_index:
                    # Mark as ambiguous by clearing the bare mapping.
                    self._tool_index[tool_name] = ""
                else:
                    self._tool_index[tool_name] = name
        return self

    def describe(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "servers": [s.describe() for s in self.servers.values()],
        }

    def list_tools(self) -> list[dict[str, Any]]:
        out = []
        for srv in self.servers.values():
            for t in srv.tools.values():
                out.append(
                    {
                        "qualified": f"{srv.name}.{t.name}",
                        "name": t.name,
                        "server": srv.name,
                        "description": t.description,
                        "input_schema": t.input_schema,
                    }
                )
        return out

    def _resolve(self, tool: str) -> tuple[Optional[str], Optional[str]]:
        """Return (server_name, tool_name) or (None, None) if not found."""
        if "." in tool:
            server_name, tool_name = tool.split(".", 1)
            return server_name, tool_name
        bare = self._tool_index.get(tool)
        if bare is None:
            return None, None
        if bare == "":
            return "", tool
        return bare, tool

    async def dispatch(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(tool, str) or not tool:
            return err("tool name required", code="invalid_input")
        if not isinstance(args, dict):
            return err("args must be an object", code="invalid_input")

        server_name, tool_name = self._resolve(tool)
        if server_name is None and tool_name is None:
            return err(
                f"unknown tool: {tool!r}",
                code="unknown_tool",
                available=[t["qualified"] for t in self.list_tools()],
            )
        if server_name == "":
            return err(
                f"ambiguous tool {tool!r}; qualify as <server>.{tool}",
                code="ambiguous",
                candidates=[s.name for s in self.servers.values() if tool in s.tools],
            )
        srv = self.servers.get(server_name or "")
        if srv is None:
            return err(
                f"unknown server: {server_name!r}",
                code="unknown_server",
                available=list(self.servers),
            )
        return await srv.dispatch(tool_name or "", args)


# ─── Module-level convenience ─────────────────────────────────────────────


_singleton: Optional[Router] = None


def get_router(config_path: Optional[Path] = None, reload: bool = False) -> Router:
    global _singleton
    if _singleton is None or reload:
        path = Path(config_path) if config_path else DEFAULT_CONFIG
        _singleton = Router(config_path=path).load()
    return _singleton


async def dispatch(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return await get_router().dispatch(tool, args)


# ─── CLI ──────────────────────────────────────────────────────────────────


def _main() -> None:  # pragma: no cover
    import argparse
    import asyncio
    import sys

    parser = argparse.ArgumentParser(prog="mcp.router")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="List all available tools")
    p_call = sub.add_parser("call", help="Invoke a tool")
    p_call.add_argument("tool")
    p_call.add_argument("--args", default="{}", help="JSON object of arguments")
    args = parser.parse_args()

    router = get_router()
    if args.cmd == "list":
        json.dump(router.list_tools(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    if args.cmd == "call":
        payload = json.loads(args.args)
        result = asyncio.run(router.dispatch(args.tool, payload))
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return


if __name__ == "__main__":  # pragma: no cover
    _main()
