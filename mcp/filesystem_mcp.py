"""MCP server: filesystem.

Safe-by-default read/write/list/stat/delete. Every path is validated:
  * Resolved to an absolute realpath (no symlink escapes).
  * Confined to JARVIS_FS_ALLOWED_ROOTS (default: project root + /tmp/jarvis).
  * Black-listed names refused (private keys, wallets, /proc/*/mem, etc.).
  * Optional max file size and binary-safety guards.

Returns the standard {"ok", "data", "error"} envelope.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from ._common import (
    Server,
    err,
    ok,
    require,
    require_dict,
    require_in,
    require_int,
    require_str,
    safe_truncate,
)


server = Server(name="filesystem")


# ─── Path policy ──────────────────────────────────────────────────────────


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _allowed_roots() -> list[Path]:
    raw = os.environ.get("JARVIS_FS_ALLOWED_ROOTS")
    if raw:
        roots = [Path(p).expanduser().resolve() for p in raw.split(":") if p.strip()]
    else:
        roots = [
            _PROJECT_ROOT,
            Path("/tmp/jarvis").resolve(),
            (Path.home() / ".cache/jarvis").resolve(),
            (Path.home() / ".local/share/jarvis").resolve(),
        ]
    return [r for r in roots if r]


_BLACK_PATTERNS = (
    "/proc/", "/sys/",  # kernel surfaces
    "/dev/sd", "/dev/nvme", "/dev/mmcblk",
    "/.ssh/id_", "/.gnupg/private-keys",
    "/boot/",
)

_BLACK_SUFFIXES = (".kdbx", ".keychain")


def _validate_path(raw: str, *, must_exist: bool = False) -> Path:
    require_str(raw, "path", max_len=4096)
    p = Path(raw).expanduser()
    try:
        resolved = p.resolve(strict=False)
    except (OSError, RuntimeError) as e:
        raise ValueError(f"path resolution failed: {e}")
    s = str(resolved)

    for bad in _BLACK_PATTERNS:
        if bad in s:
            raise ValueError(f"black-listed path: {bad}")
    for suf in _BLACK_SUFFIXES:
        if s.endswith(suf):
            raise ValueError(f"black-listed suffix: {suf}")

    roots = _allowed_roots()
    inside_allowed = any(
        resolved == r or r in resolved.parents for r in roots
    )
    if not inside_allowed:
        raise ValueError(
            f"path outside JARVIS_FS_ALLOWED_ROOTS: {resolved} "
            f"(roots: {[str(r) for r in roots]})"
        )

    if must_exist:
        require(resolved.exists(), f"path does not exist: {resolved}")
    return resolved


def _max_bytes() -> int:
    return int(os.environ.get("JARVIS_FS_MAX_BYTES", str(8 * 1024 * 1024)))  # 8 MB


# ─── Tools ────────────────────────────────────────────────────────────────


@server.tool(
    "read_file",
    description="Read a UTF-8 (or base64) file inside the allowed roots.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "encoding": {"type": "string", "enum": ["utf-8", "base64"]},
            "max_bytes": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
    },
)
async def read_file(args: dict[str, Any]) -> dict[str, Any]:
    encoding = require_in(args.get("encoding", "utf-8"), "encoding",
                          ["utf-8", "base64"])
    cap = require_int(int(args.get("max_bytes", _max_bytes())),
                      "max_bytes", lo=1, hi=64 * 1024 * 1024)
    try:
        path = _validate_path(args["path"], must_exist=True)
    except (KeyError, ValueError) as e:
        return err(str(e), code="invalid_path")
    if not path.is_file():
        return err(f"not a regular file: {path}", code="not_a_file")
    size = path.stat().st_size
    if size > cap:
        return err(f"file too large: {size} > {cap}", code="too_large",
                   size=size, max_bytes=cap)
    blob = path.read_bytes()
    if encoding == "utf-8":
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            return err("file is not valid UTF-8; request encoding=base64",
                       code="not_utf8")
        return ok({"path": str(path), "size": size, "content": text,
                   "encoding": "utf-8"})
    return ok({"path": str(path), "size": size,
               "content": base64.b64encode(blob).decode("ascii"),
               "encoding": "base64"})


@server.tool(
    "write_file",
    description=(
        "Write a file inside the allowed roots. Refuses to overwrite "
        "unless overwrite=true."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "encoding": {"type": "string", "enum": ["utf-8", "base64"]},
            "overwrite": {"type": "boolean"},
            "mode": {"type": "integer"},
        },
        "required": ["path", "content"],
    },
)
async def write_file(args: dict[str, Any]) -> dict[str, Any]:
    encoding = require_in(args.get("encoding", "utf-8"), "encoding",
                          ["utf-8", "base64"])
    overwrite = bool(args.get("overwrite", False))
    mode = int(args.get("mode", 0o644))
    content = args.get("content")
    require(isinstance(content, str), "content must be a string")
    try:
        path = _validate_path(args["path"])
    except (KeyError, ValueError) as e:
        return err(str(e), code="invalid_path")
    if path.exists() and not overwrite:
        return err(f"refusing to overwrite: {path}", code="exists",
                   path=str(path))
    blob = (content.encode("utf-8") if encoding == "utf-8"
            else base64.b64decode(content))
    cap = _max_bytes()
    if len(blob) > cap:
        return err(f"payload too large: {len(blob)} > {cap}",
                   code="too_large", size=len(blob), max_bytes=cap)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return ok({"path": str(path), "bytes_written": len(blob),
               "overwrote": overwrite})


@server.tool(
    "list_dir",
    description="Non-recursive directory listing.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "show_hidden": {"type": "boolean"},
        },
        "required": ["path"],
    },
)
async def list_dir(args: dict[str, Any]) -> dict[str, Any]:
    show_hidden = bool(args.get("show_hidden", False))
    try:
        path = _validate_path(args["path"], must_exist=True)
    except (KeyError, ValueError) as e:
        return err(str(e), code="invalid_path")
    if not path.is_dir():
        return err(f"not a directory: {path}", code="not_a_dir")
    entries = []
    for child in sorted(path.iterdir(), key=lambda c: c.name):
        if child.name.startswith(".") and not show_hidden:
            continue
        try:
            st = child.stat()
            entries.append({
                "name": child.name,
                "kind": "dir" if child.is_dir() else
                        "file" if child.is_file() else
                        "symlink" if child.is_symlink() else "other",
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            })
        except OSError:
            continue
    return ok({"path": str(path), "entries": entries, "count": len(entries)})


@server.tool(
    "stat",
    description="Stat a path: size, mtime, kind, mode.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)
async def stat(args: dict[str, Any]) -> dict[str, Any]:
    try:
        path = _validate_path(args["path"], must_exist=True)
    except (KeyError, ValueError) as e:
        return err(str(e), code="invalid_path")
    st = path.stat()
    return ok({
        "path": str(path),
        "kind": "dir" if path.is_dir() else
                "file" if path.is_file() else
                "symlink" if path.is_symlink() else "other",
        "size": st.st_size,
        "mode": oct(st.st_mode & 0o7777),
        "mtime": int(st.st_mtime),
        "ctime": int(st.st_ctime),
    })


@server.tool(
    "delete",
    description="Delete a file inside the allowed roots. Refuses directories unless recursive=true.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "recursive": {"type": "boolean"},
        },
        "required": ["path"],
    },
)
async def delete(args: dict[str, Any]) -> dict[str, Any]:
    recursive = bool(args.get("recursive", False))
    try:
        path = _validate_path(args["path"], must_exist=True)
    except (KeyError, ValueError) as e:
        return err(str(e), code="invalid_path")
    try:
        if path.is_dir():
            if not recursive:
                return err("path is a directory; pass recursive=true",
                           code="is_dir")
            # Safe rmtree: refuse to touch the project root itself.
            require(path != _PROJECT_ROOT,
                    "refusing to delete project root")
            import shutil
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as e:
        return err(f"delete failed: {e}", code="os_error")
    return ok({"path": str(path), "deleted": True})


# ─── Module entry ─────────────────────────────────────────────────────────


def get_server() -> Server:
    return server
