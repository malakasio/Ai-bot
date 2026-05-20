"""Voice pipeline: raw audio → Deepgram Nova-3 → Claude → ElevenLabs Flash v2.5.

Latency budget for Time-to-First-Audio (TTFA) is sub-500 ms. The
architecture that gets there:

  client mic → ws.bytes → DG Nova-3 (streaming, interim_results)
                            │
                            │ on first finalized utterance
                            ▼
                       Claude streaming (messages.stream)
                            │
                            │ on first ~30–60 chars of text
                            ▼
                       ElevenLabs Flash v2.5 (input-streaming WS)
                            │
                            ▼
                       audio chunks ◀── pushed to client as they arrive

Key choices for latency:

* Deepgram is configured with ``interim_results=true`` and a low
  ``endpointing`` so a finalized transcript ships milliseconds after the
  user stops speaking.
* The LLM call uses streaming so we can start TTS on the first text
  delta instead of waiting for the full response.
* ElevenLabs' WS input-streaming endpoint takes text chunks as they
  arrive and emits audio chunks immediately; we do not buffer the full
  reply on either end.

Every external call is wrapped: a missing SDK or bad key surfaces as a
PipelineError, never an import-time crash. Unit tests in this repo
exercise the assembly logic with stub providers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Protocol


# ─── Constants ────────────────────────────────────────────────────────────

TTFA_TARGET_MS = 500

DEFAULT_STT_MODEL = "nova-3"
DEFAULT_LLM_MODEL = "claude-haiku-4-5"   # picked for sub-500 ms TTFT
DEFAULT_TTS_MODEL = "eleven_flash_v2_5"
DEFAULT_TTS_VOICE = "21m00Tcm4TlvDq8ikWAM"  # Rachel — public default
DEFAULT_AUDIO_FORMAT = "linear16"           # 16-bit PCM @ 16 kHz mono in
DEFAULT_AUDIO_RATE = 16_000

# When the assistant has streamed at least this many chars of text, start
# pushing it into TTS. Smaller = lower TTFA, but more TTS overhead per turn.
TTS_FLUSH_MIN_CHARS = 24


# ─── Logging ──────────────────────────────────────────────────────────────


def _logger() -> logging.Logger:
    try:
        from core import agent as _agent
        return _agent.get_logger()
    except Exception:
        log = logging.getLogger("jarvis.voice")
        if not log.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            log.addHandler(h)
            log.setLevel(logging.INFO)
        return log


class PipelineError(RuntimeError):
    """All voice-pipeline failures go through this single type."""


# ─── Provider protocols ───────────────────────────────────────────────────


class STTProvider(Protocol):
    """Streaming STT.

    ``audio_chunks`` is an async iterator of raw PCM bytes. The provider
    yields ``(text, is_final)`` events; ``is_final=True`` means the
    transcript is committed and the next call to send() starts a new
    utterance.
    """
    async def stream(
        self, audio_chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[tuple[str, bool]]: ...


class LLMProvider(Protocol):
    """Streaming LLM text generation. Yields text deltas."""
    async def stream(
        self, prompt: str, *, system: Optional[str] = None
    ) -> AsyncIterator[str]: ...


class TTSProvider(Protocol):
    """Streaming TTS. Text chunks in, audio bytes out."""
    async def stream(
        self, text_chunks: AsyncIterator[str]
    ) -> AsyncIterator[bytes]: ...


# ─── Deepgram Nova-3 (streaming STT) ──────────────────────────────────────


class DeepgramSTT:
    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: str = DEFAULT_STT_MODEL,
        language: str = "en",
        sample_rate: int = DEFAULT_AUDIO_RATE,
        encoding: str = DEFAULT_AUDIO_FORMAT,
        endpointing_ms: int = 300,
        interim_results: bool = True,
    ) -> None:
        self.api_key = api_key or os.environ.get("DEEPGRAM_API_KEY", "").strip()
        if not self.api_key:
            raise PipelineError("DEEPGRAM_API_KEY not set")
        self.model = model
        self.language = language
        self.sample_rate = sample_rate
        self.encoding = encoding
        self.endpointing_ms = endpointing_ms
        self.interim_results = interim_results

    async def stream(
        self, audio_chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[tuple[str, bool]]:
        try:
            import websockets
        except ImportError as e:
            raise PipelineError(
                "websockets not installed; pip install websockets") from e

        params = (
            f"model={self.model}"
            f"&language={self.language}"
            f"&encoding={self.encoding}"
            f"&sample_rate={self.sample_rate}"
            f"&channels=1"
            f"&interim_results={'true' if self.interim_results else 'false'}"
            f"&endpointing={self.endpointing_ms}"
            f"&smart_format=true"
        )
        url = f"wss://api.deepgram.com/v1/listen?{params}"
        headers = [("Authorization", f"Token {self.api_key}")]
        log = _logger()

        async with websockets.connect(url, additional_headers=headers,
                                      max_size=8 * 1024 * 1024) as ws:
            async def _pump_audio() -> None:
                try:
                    async for chunk in audio_chunks:
                        if not chunk:
                            continue
                        await ws.send(chunk)
                    # Empty frame signals end-of-utterance to Deepgram.
                    await ws.send(b"")
                except Exception as e:
                    log.warning("voice.stt.pump_failed", extra={"exc": repr(e)})

            pump = asyncio.create_task(_pump_audio())
            try:
                async for raw in ws:
                    if isinstance(raw, (bytes, bytearray)):
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    channel = msg.get("channel") or {}
                    alts = channel.get("alternatives") or []
                    if not alts:
                        continue
                    text = alts[0].get("transcript") or ""
                    if not text:
                        continue
                    is_final = bool(msg.get("is_final") or msg.get("speech_final"))
                    yield text, is_final
            finally:
                pump.cancel()
                try:
                    await pump
                except Exception:
                    pass


# ─── Claude streaming LLM ────────────────────────────────────────────────


class ClaudeStreamLLM:
    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: str = DEFAULT_LLM_MODEL,
        max_tokens: int = 512,
        system: Optional[str] = None,
    ) -> None:
        # Re-use the agent's credential resolver if present so systemd
        # LoadCredential works the same way for the voice path.
        if api_key is None:
            try:
                from core import agent as _agent
                api_key = _agent._load_api_key()
            except Exception:
                api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise PipelineError("Anthropic API key not configured")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.system = system

    async def stream(
        self, prompt: str, *, system: Optional[str] = None
    ) -> AsyncIterator[str]:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:
            raise PipelineError(
                "anthropic SDK missing; pip install anthropic") from e
        client = AsyncAnthropic(api_key=self.api_key)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        sys_p = system or self.system
        if sys_p:
            kwargs["system"] = sys_p
        async with client.messages.stream(**kwargs) as stream:
            async for delta in stream.text_stream:
                if delta:
                    yield delta


# ─── ElevenLabs Flash v2.5 (streaming TTS) ───────────────────────────────


class ElevenLabsTTS:
    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        voice_id: Optional[str] = None,
        model: str = DEFAULT_TTS_MODEL,
        output_format: str = "pcm_16000",
        optimize_streaming_latency: int = 4,
    ) -> None:
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not self.api_key:
            raise PipelineError("ELEVENLABS_API_KEY not set")
        self.voice_id = (voice_id
                         or os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
                         or DEFAULT_TTS_VOICE)
        self.model = model
        self.output_format = output_format
        self.optimize_streaming_latency = optimize_streaming_latency

    async def stream(
        self, text_chunks: AsyncIterator[str]
    ) -> AsyncIterator[bytes]:
        try:
            import websockets
        except ImportError as e:
            raise PipelineError(
                "websockets not installed; pip install websockets") from e

        # Input-streaming endpoint — sends text fragments, receives audio.
        url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/"
            f"stream-input?model_id={self.model}"
            f"&output_format={self.output_format}"
            f"&optimize_streaming_latency={self.optimize_streaming_latency}"
        )
        headers = [("xi-api-key", self.api_key)]
        log = _logger()

        async with websockets.connect(url, additional_headers=headers,
                                      max_size=8 * 1024 * 1024) as ws:
            # Initial bos message with default voice settings.
            await ws.send(json.dumps({
                "text": " ",
                "voice_settings": {
                    "stability": 0.4, "similarity_boost": 0.8,
                    "speed": 1.0,
                },
                "generation_config": {
                    "chunk_length_schedule": [50, 90, 140, 200],
                },
            }))

            async def _pump_text() -> None:
                try:
                    async for chunk in text_chunks:
                        if not chunk:
                            continue
                        await ws.send(json.dumps({"text": chunk,
                                                  "try_trigger_generation": True}))
                    # End-of-stream sentinel.
                    await ws.send(json.dumps({"text": ""}))
                except Exception as e:
                    log.warning("voice.tts.pump_failed", extra={"exc": repr(e)})

            pump = asyncio.create_task(_pump_text())
            try:
                async for raw in ws:
                    if isinstance(raw, (bytes, bytearray)):
                        # Some servers send raw frames.
                        yield bytes(raw)
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    audio_b64 = msg.get("audio")
                    if audio_b64:
                        try:
                            yield base64.b64decode(audio_b64)
                        except Exception:
                            continue
                    if msg.get("isFinal"):
                        break
            finally:
                pump.cancel()
                try:
                    await pump
                except Exception:
                    pass


# ─── The pipeline ────────────────────────────────────────────────────────


@dataclass
class PipelineMetrics:
    audio_started_at: Optional[float] = None    # first audio byte from client
    transcript_at: Optional[float] = None       # first final transcript
    llm_first_token_at: Optional[float] = None
    tts_first_audio_at: Optional[float] = None
    finished_at: Optional[float] = None

    def ttfa_ms(self) -> Optional[int]:
        if self.audio_started_at and self.tts_first_audio_at:
            return int((self.tts_first_audio_at - self.audio_started_at) * 1000)
        return None

    def asdict(self) -> dict[str, Any]:
        return {
            "audio_started_at": self.audio_started_at,
            "transcript_at": self.transcript_at,
            "llm_first_token_at": self.llm_first_token_at,
            "tts_first_audio_at": self.tts_first_audio_at,
            "finished_at": self.finished_at,
            "ttfa_ms": self.ttfa_ms(),
            "ttfa_target_ms": TTFA_TARGET_MS,
        }


# Listener type the websocket layer plugs in to get pipeline events.
EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class VoicePipeline:
    stt: STTProvider
    llm: LLMProvider
    tts: TTSProvider
    system: Optional[str] = None
    on_event: Optional[EventCallback] = None
    metrics: PipelineMetrics = field(default_factory=PipelineMetrics)

    @classmethod
    def default(cls, *, system: Optional[str] = None) -> "VoicePipeline":
        """Build a pipeline from env-configured providers."""
        return cls(
            stt=DeepgramSTT(),
            llm=ClaudeStreamLLM(system=system),
            tts=ElevenLabsTTS(),
            system=system,
        )

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.on_event:
            try:
                await self.on_event(event, payload)
            except Exception as e:
                _logger().warning("voice.event.callback_failed",
                                  extra={"event": event, "exc": repr(e)})

    async def run_turn(
        self,
        audio_in: AsyncIterator[bytes],
        audio_out: Callable[[bytes], Awaitable[None]],
    ) -> dict[str, Any]:
        """Run one user→assistant turn.

        ``audio_in`` is an async iterator of raw PCM frames from the client.
        ``audio_out`` is awaited with each TTS audio chunk as it arrives.
        Returns a structured summary dict with metrics, final transcript,
        and the LLM response text.
        """
        log = _logger()
        m = self.metrics

        # The pipeline runs three coroutines concurrently:
        #   1) STT consumer: read transcripts.
        #   2) LLM streamer: when a final transcript arrives, start streaming
        #      tokens.
        #   3) TTS streamer: pump LLM deltas into TTS, push audio out.

        transcript_q: asyncio.Queue[tuple[str, bool]] = asyncio.Queue()
        text_to_tts: asyncio.Queue[Optional[str]] = asyncio.Queue()
        final_transcript = ""
        assistant_text: list[str] = []

        async def _tap_audio_in() -> AsyncIterator[bytes]:
            async for chunk in audio_in:
                if m.audio_started_at is None and chunk:
                    m.audio_started_at = time.monotonic()
                yield chunk

        async def _stt_consumer() -> None:
            nonlocal final_transcript
            async for text, is_final in self.stt.stream(_tap_audio_in()):
                await self._emit("stt", {"text": text, "is_final": is_final})
                if is_final and text.strip():
                    if m.transcript_at is None:
                        m.transcript_at = time.monotonic()
                    final_transcript = text.strip()
                    await transcript_q.put((text.strip(), True))
                    break  # one turn = one final transcript

        async def _llm_streamer() -> None:
            text, _ = await transcript_q.get()
            log.info("voice.llm.start", extra={"prompt_chars": len(text)})
            buffered = []
            async for delta in self.llm.stream(text, system=self.system):
                if m.llm_first_token_at is None:
                    m.llm_first_token_at = time.monotonic()
                assistant_text.append(delta)
                buffered.append(delta)
                joined = "".join(buffered)
                # Flush to TTS when we've accumulated enough text for a
                # clean speech chunk; this is the knob that trades TTFA
                # against prosody.
                if len(joined) >= TTS_FLUSH_MIN_CHARS or "\n" in delta:
                    await text_to_tts.put(joined)
                    buffered.clear()
            tail = "".join(buffered)
            if tail:
                await text_to_tts.put(tail)
            await text_to_tts.put(None)  # sentinel

        async def _text_iter() -> AsyncIterator[str]:
            while True:
                chunk = await text_to_tts.get()
                if chunk is None:
                    return
                yield chunk

        async def _tts_streamer() -> None:
            async for audio in self.tts.stream(_text_iter()):
                if m.tts_first_audio_at is None:
                    m.tts_first_audio_at = time.monotonic()
                    ttfa = m.ttfa_ms()
                    log.info("voice.ttfa", extra={
                        "ttfa_ms": ttfa, "target_ms": TTFA_TARGET_MS,
                        "ok": (ttfa is not None and ttfa <= TTFA_TARGET_MS),
                    })
                    await self._emit("ttfa", {"ttfa_ms": ttfa})
                try:
                    await audio_out(audio)
                except Exception as e:
                    log.warning("voice.audio_out.fail",
                                extra={"exc": repr(e)})
                    return

        await asyncio.gather(
            _stt_consumer(),
            _llm_streamer(),
            _tts_streamer(),
            return_exceptions=False,
        )

        m.finished_at = time.monotonic()
        result = {
            "transcript": final_transcript,
            "response": "".join(assistant_text),
            "metrics": m.asdict(),
        }
        await self._emit("turn_done", result)
        return result
