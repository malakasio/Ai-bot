"""FastAPI WebSocket server exposing the voice pipeline to clients.

Wire protocol (binary + text frames on a single WebSocket):

  client → server  raw audio bytes (PCM 16-bit, 16 kHz mono by default)
  client → server  JSON text frames for control:
                     {"type":"start"}    open a new turn (optional)
                     {"type":"stop"}     ask server to flush and end turn
  server → client  audio chunks as binary frames (PCM, same rate)
  server → client  JSON text frames for events:
                     {"type":"stt","text":..., "is_final":bool}
                     {"type":"ttfa","ttfa_ms":int}
                     {"type":"turn_done","transcript":..., "response":...,"metrics":{...}}
                     {"type":"error","error":str}

A connection can serve multiple turns. Each turn ends when the STT emits
a final transcript and the TTS finishes; the connection then waits for
the next batch of audio. The client may explicitly send {"type":"stop"}
to force the current turn to flush early.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional


def _logger() -> logging.Logger:
    try:
        from core import agent as _agent
        return _agent.get_logger()
    except Exception:
        log = logging.getLogger("jarvis.voice.ws")
        if not log.handlers:
            log.addHandler(logging.StreamHandler())
            log.setLevel(logging.INFO)
        return log


# ─── Connection ──────────────────────────────────────────────────────────


@dataclass
class Connection:
    id: str
    ws: Any                           # FastAPI WebSocket
    audio_in: asyncio.Queue          # bytes frames
    user_id: Optional[str] = None
    opened_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    closed: bool = False


class ConnectionManager:
    """Per-process connection registry."""

    def __init__(self, *, max_connections: int = 64,
                 idle_timeout_s: float = 300.0) -> None:
        self.max_connections = max_connections
        self.idle_timeout_s = idle_timeout_s
        self._conns: dict[str, Connection] = {}
        self._lock = asyncio.Lock()

    async def register(self, ws: Any, user_id: Optional[str] = None
                       ) -> Connection:
        async with self._lock:
            if len(self._conns) >= self.max_connections:
                raise RuntimeError("max websocket connections reached")
            cid = uuid.uuid4().hex[:12]
            conn = Connection(
                id=cid, ws=ws,
                audio_in=asyncio.Queue(maxsize=256),
                user_id=user_id,
            )
            self._conns[cid] = conn
        _logger().info("voice.ws.connect", extra={
            "conn_id": cid, "active": len(self._conns)})
        return conn

    async def unregister(self, conn: Connection) -> None:
        async with self._lock:
            self._conns.pop(conn.id, None)
        conn.closed = True
        _logger().info("voice.ws.disconnect", extra={
            "conn_id": conn.id, "active": len(self._conns),
            "lifetime_s": round(time.monotonic() - conn.opened_at, 2)})

    def count(self) -> int:
        return len(self._conns)

    def all(self) -> list[Connection]:
        return list(self._conns.values())

    async def reap_idle(self) -> int:
        """Close connections that have been idle past the timeout."""
        now = time.monotonic()
        victims: list[Connection] = []
        for conn in list(self._conns.values()):
            if now - conn.last_activity >= self.idle_timeout_s:
                victims.append(conn)
        for v in victims:
            with suppress(Exception):
                await v.ws.close(code=1000)
            await self.unregister(v)
        return len(victims)


# ─── Helpers to bridge queue → async iterator ────────────────────────────


async def _audio_iterator(conn: Connection,
                          stop_event: asyncio.Event) -> AsyncIterator[bytes]:
    """Yield audio frames until the client signals stop or the queue closes."""
    while not stop_event.is_set():
        try:
            chunk = await asyncio.wait_for(conn.audio_in.get(),
                                           timeout=10.0)
        except asyncio.TimeoutError:
            # No audio in the last 10s — treat as silence, end the turn.
            return
        if chunk is None:
            return
        conn.last_activity = time.monotonic()
        yield chunk


# ─── FastAPI app factory ────────────────────────────────────────────────


def create_app(*, system_prompt: Optional[str] = None) -> Any:
    """Build the FastAPI app. Importing this module does NOT require
    FastAPI to be installed; create_app() does."""
    try:
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    except ImportError as e:
        raise RuntimeError(
            "fastapi not installed; pip install fastapi uvicorn") from e

    from .pipeline import VoicePipeline, PipelineError

    app = FastAPI(title="JARVIS Voice WebSocket", version="7.0")
    manager = ConnectionManager(
        max_connections=int(os.environ.get("VOICE_WS_MAX_CONN", "64")),
        idle_timeout_s=float(os.environ.get("VOICE_WS_IDLE_TIMEOUT_S", "300")),
    )
    app.state.connection_manager = manager

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "active_connections": manager.count(),
            "max_connections": manager.max_connections,
        }

    @app.get("/metrics")
    async def metrics() -> dict[str, Any]:
        return {
            "active": manager.count(),
            "connections": [
                {
                    "id": c.id,
                    "user_id": c.user_id,
                    "opened_at": c.opened_at,
                    "last_activity": c.last_activity,
                }
                for c in manager.all()
            ],
        }

    @app.websocket("/voice")
    async def voice_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        try:
            conn = await manager.register(ws)
        except RuntimeError:
            await ws.close(code=1013, reason="server full")
            return

        async def _send_audio(chunk: bytes) -> None:
            with suppress(Exception):
                await ws.send_bytes(chunk)
            conn.last_activity = time.monotonic()

        async def _on_event(event: str, payload: dict[str, Any]) -> None:
            with suppress(Exception):
                await ws.send_text(json.dumps({"type": event, **payload}))

        log = _logger()

        try:
            pipeline = VoicePipeline.default(system=system_prompt)
        except PipelineError as e:
            log.error("voice.ws.pipeline_init_failed",
                      extra={"conn_id": conn.id, "exc": repr(e)})
            with suppress(Exception):
                await ws.send_text(json.dumps(
                    {"type": "error", "error": str(e)}))
            await manager.unregister(conn)
            with suppress(Exception):
                await ws.close(code=1011)
            return

        pipeline.on_event = _on_event
        stop_event = asyncio.Event()

        async def _receiver() -> None:
            """Pump WebSocket frames into the audio queue and handle control."""
            while not conn.closed:
                try:
                    msg = await ws.receive()
                except WebSocketDisconnect:
                    stop_event.set()
                    await conn.audio_in.put(None)
                    return
                except Exception as e:
                    log.warning("voice.ws.receive_failed",
                                extra={"conn_id": conn.id, "exc": repr(e)})
                    stop_event.set()
                    await conn.audio_in.put(None)
                    return

                if "bytes" in msg and msg["bytes"] is not None:
                    with suppress(asyncio.QueueFull):
                        conn.audio_in.put_nowait(msg["bytes"])
                    conn.last_activity = time.monotonic()
                elif "text" in msg and msg["text"] is not None:
                    try:
                        ctrl = json.loads(msg["text"])
                    except Exception:
                        continue
                    if ctrl.get("type") == "stop":
                        await conn.audio_in.put(None)
                    elif ctrl.get("type") == "start":
                        # New turn — drain any leftover audio.
                        while not conn.audio_in.empty():
                            conn.audio_in.get_nowait()
                elif msg.get("type") == "websocket.disconnect":
                    stop_event.set()
                    await conn.audio_in.put(None)
                    return

        async def _turn_loop() -> None:
            while not stop_event.is_set():
                try:
                    result = await pipeline.run_turn(
                        _audio_iterator(conn, stop_event),
                        _send_audio,
                    )
                    log.info("voice.ws.turn_done", extra={
                        "conn_id": conn.id,
                        "metrics": result.get("metrics", {}),
                    })
                    # Allow the client to start another turn on the same socket.
                    # Fresh pipeline so metrics reset for the next TTFA.
                    pipeline.metrics = pipeline.metrics.__class__()
                except Exception as e:
                    log.warning("voice.ws.turn_failed", extra={
                        "conn_id": conn.id, "exc": repr(e)})
                    with suppress(Exception):
                        await ws.send_text(json.dumps(
                            {"type": "error", "error": repr(e)}))
                    return

        recv_task = asyncio.create_task(_receiver())
        turn_task = asyncio.create_task(_turn_loop())
        try:
            await asyncio.wait(
                {recv_task, turn_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_event.set()
            for t in (recv_task, turn_task):
                if not t.done():
                    t.cancel()
                with suppress(Exception):
                    await t
            await manager.unregister(conn)
            with suppress(Exception):
                await ws.close()

    return app


# ─── CLI ────────────────────────────────────────────────────────────────


def main() -> None:  # pragma: no cover
    import argparse
    parser = argparse.ArgumentParser(prog="voice.websocket_server")
    parser.add_argument("--host", default=os.environ.get("VOICE_WS_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("VOICE_WS_PORT", "8080")))
    parser.add_argument("--system", default=None,
                        help="optional system prompt for the LLM turn")
    args = parser.parse_args()
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("pip install fastapi uvicorn")
    app = create_app(system_prompt=args.system)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
