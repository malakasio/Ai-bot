"""Shared helpers for MCP servers.

Common response envelope, input validation primitives, and a `Tool`
dataclass that each server uses to declare what it exposes.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


# ─── Response envelope ────────────────────────────────────────────────────


def ok(data: Any = None, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None, **extra}


def err(message: str, *, code: str = "error", **extra: Any) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": {"code": code, "message": message}, **extra}


# ─── Validation primitives ────────────────────────────────────────────────


class ValidationError(ValueError):
    """Raised by require_*; caught and turned into a structured err()."""


def require(cond: bool, message: str) -> None:
    if not cond:
        raise ValidationError(message)


def require_str(value: Any, name: str, *, max_len: int = 4096,
                pattern: Optional[str] = None) -> str:
    require(isinstance(value, str), f"{name!r} must be a string")
    require(0 < len(value) <= max_len,
            f"{name!r} must be 1..{max_len} chars (got {len(value)})")
    if pattern is not None:
        require(re.fullmatch(pattern, value) is not None,
                f"{name!r} does not match {pattern!r}")
    return value


def require_int(value: Any, name: str, *, lo: Optional[int] = None,
                hi: Optional[int] = None) -> int:
    require(isinstance(value, int) and not isinstance(value, bool),
            f"{name!r} must be an int")
    if lo is not None:
        require(value >= lo, f"{name!r} must be >= {lo}")
    if hi is not None:
        require(value <= hi, f"{name!r} must be <= {hi}")
    return value


def require_in(value: Any, name: str, choices: list[Any]) -> Any:
    require(value in choices,
            f"{name!r} must be one of {choices!r}; got {value!r}")
    return value


def require_dict(value: Any, name: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{name!r} must be an object")
    return value


def safe_truncate(s: str, n: int = 4096) -> str:
    return s if len(s) <= n else (s[:n] + f"…[+{len(s) - n} chars]")


# ─── Tool declaration ─────────────────────────────────────────────────────


Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class Tool:
    name: str
    description: str
    handler: Handler
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class Server:
    name: str
    tools: dict[str, Tool] = field(default_factory=dict)

    def tool(self, name: str, description: str = "",
             input_schema: Optional[dict[str, Any]] = None
            ) -> Callable[[Handler], Handler]:
        """Decorator: register an async handler as a tool."""
        def deco(fn: Handler) -> Handler:
            require(inspect.iscoroutinefunction(fn),
                    f"tool {name!r} handler must be async")
            self.tools[name] = Tool(
                name=name,
                description=description or (fn.__doc__ or "").strip().splitlines()[0:1] and
                            (fn.__doc__ or "").strip().splitlines()[0] or "",
                handler=fn,
                input_schema=input_schema or {},
            )
            return fn
        return deco

    async def dispatch(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = self.tools.get(tool_name)
        if tool is None:
            return err(f"unknown tool: {tool_name}", code="unknown_tool",
                       server=self.name, available=list(self.tools))
        try:
            require_dict(args, "arguments")
            result = await tool.handler(args)
            if not isinstance(result, dict) or "ok" not in result:
                return ok(result, server=self.name, tool=tool_name)
            result.setdefault("server", self.name)
            result.setdefault("tool", tool_name)
            return result
        except ValidationError as e:
            return err(str(e), code="invalid_input",
                       server=self.name, tool=tool_name)
        except Exception as e:
            return err(repr(e), code="exception",
                       server=self.name, tool=tool_name)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tools": [
                {"name": t.name, "description": t.description,
                 "input_schema": t.input_schema}
                for t in self.tools.values()
            ],
        }


def json_dumps(obj: Any) -> str:
    """Stable JSON encoder for tool responses."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
