"""JARVIS MCP server package.

Each module here defines one MCP server with a `register(router)` hook and
async tool handlers. The router (`mcp.router`) loads them from
`config/mcp_config.json` and dispatches incoming tool calls.

All handlers MUST:
  * validate every input (type, range, allowlist),
  * honor security-zone policy for any filesystem or network egress,
  * return a JSON-serializable dict that includes at minimum
    {"ok": bool, "data": ..., "error": null | str}.
"""

__all__ = [
    "filesystem_mcp",
    "network_mcp",
    "automation_mcp",
    "router",
]
