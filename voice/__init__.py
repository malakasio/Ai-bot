"""JARVIS v7.0 voice pipeline.

Two modules:

* ``voice.pipeline``        — the streaming STT → agent → TTS state machine.
* ``voice.websocket_server`` — FastAPI WebSocket endpoint and connection
                                manager that wires the pipeline to a
                                browser/mobile client.

Both modules import lazily; ``import voice`` works even when fastapi,
deepgram-sdk, httpx, or anthropic are missing — symbols only blow up at
call time.
"""

__all__ = ["pipeline", "websocket_server"]
