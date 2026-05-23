"""JARVIS Telegram bot — inbound message channel.

This module wires the Telegram Bot API to the JARVIS agent loop
(`core.agent.run_jarvis_core`) so the operator can talk to the system
from their phone.

Design constraints
------------------
* Only libraries already in ``requirements.txt`` are used. The relevant
  ones are ``python-telegram-bot`` (Bot API client), ``httpx`` (used by
  ``core.agent`` and by us for the no-PTB fallback), and ``python-dotenv``
  (loaded transitively by ``core.agent``).
* The module must be import-safe even when ``python-telegram-bot`` is
  not installed. Imports of the SDK happen at call time inside
  :func:`start_telegram_bot`. ``main.py`` registers this module as a
  supervisor factory; a missing dep results in a clean ``exit_clean``
  rather than crashing the daemon supervisor.
* Authorization is enforced by ``TELEGRAM_USER_ID``. Any message from a
  different ``user.id`` is rejected with a one-line response telling the
  operator their actual ID — so they can set the env var without
  guessing.
* Per-chat conversation history is kept in-process in a bounded LRU.
  This is intentional: the agent's own DB layer is a moving target and
  the bot must keep working even when the DB is down. History persists
  for the life of the process — long enough to be useful across one
  back-and-forth session — and is bounded so a runaway chat cannot blow
  memory.
* The bot supports two operating modes:
    ``polling`` — long-polling via ``Application.run_polling`` (default,
                  works everywhere, no inbound network needed).
    ``webhook`` — set ``TELEGRAM_WEBHOOK_URL`` and we register a webhook
                  instead. Only useful when the host is reachable from
                  the public internet.
* Every outbound message is split at paragraph boundaries when it
  exceeds Telegram's 4096-char limit. Markdown is attempted first; if
  Telegram rejects the parse we resend the same chunk as plain text.

Public surface
--------------
* :func:`start_telegram_bot` — async daemon entrypoint used by
  ``main.py``'s supervisor. Blocks for the life of the process.
* :func:`is_enabled` — quick predicate (``True`` iff both env vars are
  set). Used by ``main.py`` to decide whether to register the
  supervisor at all.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Optional


# ─── Module-level state ─────────────────────────────────────────────────────


_MAX_MESSAGE_LEN = 4000          # Telegram hard limit is 4096; leave room
_MAX_HISTORY_PER_CHAT = 20       # message pairs kept per chat
_MAX_CACHED_CHATS = 200          # LRU bound on number of distinct chats
_AGENT_TIMEOUT_S = 180.0         # cap a single agent run at 3 minutes
_MAX_PROMPT_CHARS = 8000         # truncate enormous inbound messages

# chat_id -> deque-like list of {"role": "user"|"assistant", "content": str}
_history: "OrderedDict[int, list[dict[str, str]]]" = OrderedDict()


def _log() -> logging.Logger:
    log = logging.getLogger("jarvis.telegram")
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


# ─── Configuration helpers ──────────────────────────────────────────────────


def _bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _allowed_user_id() -> Optional[int]:
    raw = (os.environ.get("TELEGRAM_USER_ID", "")
           or os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        _log().warning("invalid TELEGRAM_USER_ID=%r (must be integer)", raw)
        return None


def is_enabled() -> bool:
    """True iff both bot token and allowed user id are configured."""
    return bool(_bot_token()) and _allowed_user_id() is not None


# ─── History (bounded, in-memory) ───────────────────────────────────────────


def _get_history(chat_id: int) -> list[dict[str, str]]:
    """Return (and LRU-touch) the history list for ``chat_id``."""
    hist = _history.get(chat_id)
    if hist is None:
        hist = []
        _history[chat_id] = hist
        # Evict oldest chat if we exceed the cache bound.
        while len(_history) > _MAX_CACHED_CHATS:
            _history.popitem(last=False)
    else:
        _history.move_to_end(chat_id)
    return hist


def _append_history(chat_id: int, role: str, content: str) -> None:
    hist = _get_history(chat_id)
    hist.append({"role": role, "content": content})
    # Keep last N user+assistant turns (so 2*N entries).
    max_entries = 2 * _MAX_HISTORY_PER_CHAT
    if len(hist) > max_entries:
        del hist[: len(hist) - max_entries]


def _clear_history(chat_id: int) -> None:
    if chat_id in _history:
        _history[chat_id] = []


def _format_prompt_with_history(chat_id: int, user_text: str) -> str:
    """Inline prior turns into the prompt.

    ``run_jarvis_core`` takes a single user prompt string. Rather than
    bypass that contract, we prefix the user turn with a transcript of
    the recent conversation so the model has context. Token budgeting
    inside ``run_jarvis_core`` still applies.
    """
    hist = _get_history(chat_id)
    if not hist:
        return user_text
    lines: list[str] = ["[conversation so far]"]
    for entry in hist[-2 * _MAX_HISTORY_PER_CHAT:]:
        role = "User" if entry["role"] == "user" else "Assistant"
        # Defensive truncation per turn so a giant past message doesn't
        # dominate the budget.
        body = entry["content"]
        if len(body) > 1500:
            body = body[:1500] + "…"
        lines.append(f"{role}: {body}")
    lines.append("")
    lines.append("[current user message]")
    lines.append(user_text)
    return "\n".join(lines)


# ─── Message sending (chunked, markdown-with-fallback) ──────────────────────


_send_locks: dict[int, asyncio.Lock] = {}


def _split_message(text: str) -> list[str]:
    if len(text) <= _MAX_MESSAGE_LEN:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        # If a single line is itself too long, split it hard.
        if len(line) > _MAX_MESSAGE_LEN:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), _MAX_MESSAGE_LEN):
                chunks.append(line[i : i + _MAX_MESSAGE_LEN])
            continue
        if len(current) + len(line) + 1 > _MAX_MESSAGE_LEN:
            if current:
                chunks.append(current)
            current = line
        else:
            current = (current + "\n" + line) if current else line
    if current:
        chunks.append(current)
    return chunks


async def _send(bot, chat_id: int, text: str) -> None:
    """Send a (possibly long) message, with parse-mode fallback."""
    lock = _send_locks.setdefault(chat_id, asyncio.Lock())
    chunks = _split_message(text) or [" "]
    async with lock:
        for chunk in chunks:
            try:
                await bot.send_message(chat_id, chunk, parse_mode="Markdown")
            except Exception:
                # Telegram is strict about Markdown; on parse failure
                # we resend the same chunk without parse_mode.
                try:
                    await bot.send_message(chat_id, chunk)
                except Exception as e:
                    _log().error("telegram send failed: %r", e)
                    return
            if len(chunks) > 1:
                # Soft rate-limit between chunks (Telegram allows ~1 msg/s
                # per chat for bots).
                await asyncio.sleep(1.0)


# ─── Agent invocation ───────────────────────────────────────────────────────


async def _run_agent(chat_id: int, user_text: str) -> str:
    """Run one agent turn for ``chat_id``. Returns the assistant reply."""
    prompt = _format_prompt_with_history(chat_id, user_text)
    try:
        from core.agent import run_jarvis_core
    except Exception as e:
        return f"⚠️ Agent unavailable: {e!r}"

    # Attempt to pass MCP tools if the router is wired up. We tolerate
    # failure — the agent works fine without tools, and a broken MCP
    # router must never take the chat down.
    #
    # NOTE: Anthropic's API requires tool names to match the regex
    # ^[a-zA-Z0-9_-]{1,128}$ — dots are NOT permitted. The MCP router
    # emits qualified names like "filesystem.write_file", so we sanitize
    # by replacing "." with "__" and register a bridge handler in
    # core.agent._MCP_TOOLS that dispatches via the router using the
    # ORIGINAL qualified name. This keeps the wire format API-legal while
    # preserving routing semantics.
    tools: Optional[list[dict[str, Any]]] = None
    try:
        from mcp.router import get_router  # type: ignore
        from core.agent import register_mcp_tool  # type: ignore

        router = get_router()
        raw_tools = router.list_tools()

        def _sanitize(qualified: str) -> str:
            # Replace any character not in [A-Za-z0-9_-] with "_".
            # Dots in qualified names become "__" so we can still tell
            # server/tool apart visually in logs.
            out = []
            for ch in qualified:
                if ch.isalnum() or ch in ("_", "-"):
                    out.append(ch)
                elif ch == ".":
                    out.append("__")
                else:
                    out.append("_")
            sanitized = "".join(out)[:128]
            return sanitized or "tool"

        def _make_dispatcher(qualified_name: str):
            async def _dispatch(args: dict[str, Any]) -> Any:
                return await router.dispatch(qualified_name, args)
            return _dispatch

        tools = []
        for t in raw_tools:
            qualified = t["qualified"]
            safe = _sanitize(qualified)
            tools.append({
                "name": safe,
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema",
                                      {"type": "object",
                                       "properties": {},
                                       "additionalProperties": True}),
            })
            # Bridge: when the agent dispatches `safe`, route to the real
            # qualified name through the MCP router.
            register_mcp_tool(safe, _make_dispatcher(qualified))

        if not tools:
            tools = None
    except Exception:
        tools = None

    try:
        run = await asyncio.wait_for(
            run_jarvis_core(prompt, tools=tools),
            timeout=_AGENT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return ("⚠️ Agent timed out after "
                f"{int(_AGENT_TIMEOUT_S)}s. Try a smaller prompt or /clear.")
    except Exception as e:
        _log().exception("agent run failed")
        return f"⚠️ Agent error: {e!r}"

    reply = (run.final_message or "").strip()
    if not reply:
        reply = (
            f"(no final message — stopped: {run.stopped_reason or 'unknown'}, "
            f"iterations: {run.iterations})"
        )

    # Extract screenshots from transcript and send as photos
    screenshots = []
    for msg in run.transcript:
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_content = block.get("content")
                    if isinstance(tool_content, str):
                        try:
                            import json
                            result = json.loads(tool_content)
                            if isinstance(result, dict) and "screenshot_base64" in result:
                                screenshots.append(result["screenshot_base64"])
                        except Exception:
                            pass

    # Send screenshots via Telegram
    if screenshots:
        try:
            from telegram import Bot
            import base64
            from io import BytesIO

            bot = Bot(token=_bot_token())
            for i, b64_data in enumerate(screenshots):
                try:
                    img_bytes = base64.b64decode(b64_data)
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=BytesIO(img_bytes),
                        caption=f"Screenshot {i+1}/{len(screenshots)}" if len(screenshots) > 1 else None
                    )
                except Exception as e:
                    _log().error(f"Failed to send screenshot {i+1}: {e}")
        except Exception as e:
            _log().error(f"Failed to process screenshots: {e}")

    return reply


# ─── Bot entrypoint ─────────────────────────────────────────────────────────


async def start_telegram_bot() -> None:
    """Daemon entrypoint.

    Returns cleanly (rather than raising) when:
      * the bot is not configured — no token or no allowed user id;
      * ``python-telegram-bot`` is not installed.

    Otherwise it blocks for the life of the process, restarted on crash
    by the ``_Supervisor`` in ``main.py``.
    """
    log = _log()

    if not is_enabled():
        log.info("telegram bot disabled (no token/user_id)")
        return

    try:
        # All PTB imports happen at call time so the module is import-safe
        # without the dep installed.
        from telegram import Update  # noqa: F401  (used by handler types)
        from telegram.constants import ChatAction
        from telegram.ext import (
            Application,
            CommandHandler,
            MessageHandler,
            filters,
        )
    except Exception as e:
        log.warning("python-telegram-bot unavailable: %r", e)
        return

    token = _bot_token()
    allowed_user_id = _allowed_user_id()
    assert allowed_user_id is not None  # guarded by is_enabled()

    application = Application.builder().token(token).build()

    # ── Authorization decorator ─────────────────────────────────────────

    def auth_required(handler):
        async def wrapper(update, context):
            user = update.effective_user
            chat = update.effective_chat
            if user is None or chat is None:
                return
            if user.id != allowed_user_id:
                log.warning(
                    "unauthorized user_id=%s expected=%s",
                    user.id, allowed_user_id,
                )
                try:
                    await context.bot.send_message(
                        chat.id,
                        (f"🔒 Unauthorized. Your user ID: `{user.id}`. "
                         f"Set TELEGRAM_USER_ID={user.id} to grant access."),
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                return
            await handler(update, context)
        return wrapper

    # ── Commands ────────────────────────────────────────────────────────

    @auth_required
    async def cmd_start(update, context):
        await _send(context.bot, update.effective_chat.id,
            "🤖 *JARVIS online.*\n\n"
            "Send any text and I'll route it through the agent.\n"
            "/help for commands."
        )

    @auth_required
    async def cmd_help(update, context):
        await _send(context.bot, update.effective_chat.id,
            "*JARVIS — commands*\n\n"
            "`/start`   — greeting + status\n"
            "`/help`    — this message\n"
            "`/ping`    — health check\n"
            "`/whoami`  — your Telegram user id\n"
            "`/clear`   — clear this chat's conversation history\n"
            "`/status`  — runtime status\n\n"
            "Any other text is sent to the agent. Replies arrive in this chat."
        )

    @auth_required
    async def cmd_whoami(update, context):
        u = update.effective_user
        await _send(context.bot, update.effective_chat.id,
                    f"You are `{u.id}` ({u.username or '—'}).")

    @auth_required
    async def cmd_clear(update, context):
        _clear_history(update.effective_chat.id)
        await _send(context.bot, update.effective_chat.id,
                    "🧹 Conversation history cleared.")

    @auth_required
    async def cmd_status(update, context):
        hist = _get_history(update.effective_chat.id)
        msg = (
            "*JARVIS status*\n"
            f"• history turns: {len(hist)}\n"
            f"• tracked chats: {len(_history)}\n"
            f"• model: `{os.environ.get('JARVIS_AGENT_MODEL', 'claude-haiku-4-5')}`"
        )
        await _send(context.bot, update.effective_chat.id, msg)

    async def cmd_ping(update, context):
        # No auth on /ping — useful as an external liveness check.
        try:
            await context.bot.send_message(update.effective_chat.id,
                                           "🟢 JARVIS online")
        except Exception:
            pass

    # ── Text handler — the actual agent bridge ──────────────────────────

    @auth_required
    async def handle_text(update, context):
        chat_id = update.effective_chat.id
        text = (update.message.text or "").strip()
        if not text:
            return

        if len(text) > _MAX_PROMPT_CHARS:
            await _send(context.bot, chat_id,
                f"⚠️ Message too long ({len(text)} chars > "
                f"{_MAX_PROMPT_CHARS}). Truncating.")
            text = text[:_MAX_PROMPT_CHARS]

        # Show "typing…" so the operator knows the bot received the message.
        try:
            await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            pass

        # Record user turn before invoking the agent so /clear semantics
        # are predictable even if the agent call fails.
        _append_history(chat_id, "user", text)

        t0 = time.time()
        try:
            reply = await _run_agent(chat_id, text)
        except Exception as e:
            log.exception("handle_text crashed")
            reply = f"⚠️ Internal error: {e!r}"
        dt = time.time() - t0

        # Record assistant turn (even on error replies — useful context).
        _append_history(chat_id, "assistant", reply)

        log.info("agent.reply chat_id=%s len=%d dt=%.2fs",
                 chat_id, len(reply), dt)

        await _send(context.bot, chat_id, reply)

    # ── Error handler ───────────────────────────────────────────────────

    async def on_error(update, context):
        err = context.error
        log.error("telegram handler error: %r", err, exc_info=err)
        if update is not None and getattr(update, "effective_chat", None):
            try:
                await context.bot.send_message(
                    update.effective_chat.id,
                    f"⚠️ Internal error: {str(err)[:200]}",
                )
            except Exception:
                pass

    # ── Register handlers ───────────────────────────────────────────────

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("ping", cmd_ping))
    application.add_handler(CommandHandler("whoami", cmd_whoami))
    application.add_handler(CommandHandler("clear", cmd_clear))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    application.add_error_handler(on_error)

    # Drop any pending webhook so polling can start cleanly.
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning("delete_webhook failed (continuing): %r", e)

    webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()
    log.info("telegram bot starting (user_id=%s, mode=%s)",
             allowed_user_id, "webhook" if webhook_url else "polling")

    await application.initialize()
    await application.start()
    try:
        if webhook_url:
            # Webhook mode: register, then idle. The PTB Application
            # accepts incoming requests via the registered webhook URL;
            # the caller is responsible for routing HTTP traffic to PTB.
            await application.bot.set_webhook(
                url=webhook_url,
                drop_pending_updates=True,
            )
            # Keep the daemon alive until cancelled by the supervisor.
            stop_event = asyncio.Event()
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                raise
        else:
            await application.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=None,
            )
            # Block until cancelled.
            stop_event = asyncio.Event()
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                raise
    finally:
        # Shut down gracefully — the supervisor will call back into us
        # via task cancellation when the lifespan tears down.
        try:
            if application.updater and application.updater.running:
                await application.updater.stop()
        except Exception:
            pass
        try:
            await application.stop()
        except Exception:
            pass
        try:
            await application.shutdown()
        except Exception:
            pass
        log.info("telegram bot stopped")
