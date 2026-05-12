"""
Telegram Bot — full-power mobile interface for JARVIS.

Commands:
/start         - Welcome + status
/status        - System health
/memory [q]    - Search memories
/remind <when> <text> - Schedule reminder (e.g. /remind σε 2 ώρες πάρε τηλέφωνο)
/search <q>    - Web search
/browse <url>  - Fetch & summarise a webpage
/run <code>    - Execute Python code
/clear         - Clear conversation session
/cost          - API costs today
/logs [n]      - Last N log lines
/stop          - Pause background tasks
/skill_proposals - Review AI self-improvement proposals
/help          - This message

Media handlers:
- Voice messages  → Groq Whisper transcription → full agent
- Photos          → vision description → full agent
- Plain text      → full agent with session memory
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional

from jarvis.config import get_config
from jarvis.observability.logger import get_logger

log = get_logger("telegram")

_send_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def send_safe(bot, chat_id: int, text: str, parse_mode: str = "Markdown"):
    """Send a message, splitting at paragraph boundaries if > 4000 chars."""
    MAX = 4000
    lock = _send_locks[chat_id]
    async with lock:
        chunks: list[str] = []
        if len(text) <= MAX:
            chunks = [text]
        else:
            current = ""
            for line in text.split("\n"):
                if len(current) + len(line) + 1 > MAX:
                    if current:
                        chunks.append(current)
                    current = line
                else:
                    current += ("\n" if current else "") + line
            if current:
                chunks.append(current)

        for chunk in chunks:
            try:
                await bot.send_message(chat_id, chunk, parse_mode=parse_mode)
            except Exception:
                # Retry without markdown (handles parse errors); other errors propagate
                await bot.send_message(chat_id, chunk)
            if len(chunks) > 1:
                await asyncio.sleep(1.1)


async def _typing(bot, chat_id: int):
    """Show 'typing...' indicator."""
    try:
        await bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass


async def _tts_bytes(text: str) -> bytes | None:
    """Generate TTS audio with edge-tts. Returns MP3 bytes or None on failure."""
    try:
        import edge_tts, io
        cfg = get_config()
        communicate = edge_tts.Communicate(text, voice=cfg.voice.edge_tts_voice)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue() if buf.tell() > 0 else None
    except Exception:
        return None



# Per-session agent cache — avoids re-initialization on every message
_agent_cache: dict[str, tuple[object, float]] = {}  # session_id → (agent, last_used_ts)
_AGENT_CACHE_TTL = 1800  # 30 minutes


def _get_cached_agent(session_id: str):
    """Return cached agent or None if expired/missing."""
    now = time.time()
    # Evict stale entries
    stale = [k for k, (_, ts) in _agent_cache.items() if now - ts > _AGENT_CACHE_TTL]
    for k in stale:
        del _agent_cache[k]
    entry = _agent_cache.get(session_id)
    if entry:
        _agent_cache[session_id] = (entry[0], now)  # refresh TTL
        return entry[0]
    return None


async def _run_agent(text: str, session_id: str) -> str:
    """Run the full agent with session memory and all tools."""
    from jarvis.agents.base import BaseAgent
    from jarvis.agents.orchestrator import CoordinatorAgent
    from jarvis.tools.registry import get_tools_for_set
    from jarvis.memory.store import save_session_message, load_session_messages
    from jarvis.llm.router import classify_task_by_keywords
    import jarvis.daemon.kairos as _kairos_mod

    _kairos_mod._last_activity_time = time.time()

    history = await load_session_messages(session_id)
    tool_defs, handlers = get_tools_for_set([])

    task_type = classify_task_by_keywords(text)
    is_complex = task_type in ("architecture", "deep_debug", "critical", "code_generation", "analysis")

    # Reuse cached agent to skip re-initialization overhead
    agent = _get_cached_agent(session_id)
    if agent is None or is_complex:
        if is_complex:
            agent = CoordinatorAgent()
        else:
            agent = BaseAgent(agent_id=f"tg_{session_id[-8:]}")
        await agent.initialize()
        for td in tool_defs:
            agent.register_tool(td["name"], td["description"], td["input_schema"], handlers[td["name"]])
        if not is_complex:
            _agent_cache[session_id] = (agent, time.time())

    agent.prior_messages = [
        {"role": m["role"], "content": m["content"][:500]}
        for m in history[-6:]
        if m["role"] in ("user", "assistant")
    ]

    await save_session_message(session_id, "user", text)

    if isinstance(agent, CoordinatorAgent):
        result = await agent.run_orchestrated(text)
    else:
        result = await agent.run_task(text)

    response = result.output if result.success else f"⚠️ {result.error or 'Unknown error'}"
    if response:
        await save_session_message(session_id, "assistant", response)
    return response or "(no response)"


async def _transcribe_groq(audio_bytes: bytes, ext: str = "ogg") -> str:
    """Transcribe audio using Groq's free Whisper API."""
    import httpx
    cfg = get_config()
    if not cfg.llm.groq_api_key:
        return "[Χρειάζεται GROQ_API_KEY για μεταγραφή φωνής]"
    try:
        mime = "audio/ogg" if ext in ("ogg", "oga") else "audio/mpeg"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {cfg.llm.groq_api_key}"},
                files={"file": (f"audio.{ext}", audio_bytes, mime)},
                data={"model": "whisper-large-v3", "language": "el"},
            )
        if resp.status_code == 200:
            return resp.json().get("text", "").strip()
        return f"[Transcription error {resp.status_code}: {resp.text[:200]}]"
    except Exception as e:
        return f"[Transcription failed: {e}]"


def _parse_remind_time(args: list[str]) -> tuple[float | None, str]:
    """
    Parse reminder time from args like ['σε', '2', 'ώρες', 'πάρε', 'τηλέφωνο'].
    Returns (timestamp_or_None, reminder_text).
    """
    text = " ".join(args)
    now = time.time()

    patterns = [
        (r"σε\s+(\d+)\s*λεπτ", lambda m: now + int(m.group(1)) * 60),
        (r"σε\s+(\d+)\s*ώρ", lambda m: now + int(m.group(1)) * 3600),
        (r"σε\s+(\d+)\s*μέρ", lambda m: now + int(m.group(1)) * 86400),
        (r"in\s+(\d+)\s*min", lambda m: now + int(m.group(1)) * 60),
        (r"in\s+(\d+)\s*hour", lambda m: now + int(m.group(1)) * 3600),
        (r"in\s+(\d+)\s*day", lambda m: now + int(m.group(1)) * 86400),
    ]

    for pattern, fn in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            trigger_ts = fn(m)
            reminder_text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip(" ,")
            return trigger_ts, reminder_text

    return None, text


# ─── Bot startup ──────────────────────────────────────────────────────────────

async def start_telegram_bot():
    """Start the Telegram bot polling."""
    cfg = get_config()
    if not cfg.telegram.enabled:
        log.info("Telegram bot disabled (no token/user_id)")
        return

    try:
        from telegram import Update
        from telegram.ext import (
            Application, CommandHandler, MessageHandler, filters
        )
    except ImportError:
        log.error("python-telegram-bot not installed")
        return

    app = Application.builder().token(cfg.telegram.bot_token).build()

    def auth_required(func):
        async def wrapper(update, context):
            user = update.effective_user
            if user is None:
                return
            if user.id != cfg.telegram.allowed_user_id:
                log.warning(f"Unauthorized: user_id={user.id}, expected={cfg.telegram.allowed_user_id}")
                # Tell the user their ID so they can fix TELEGRAM_USER_ID in Railway
                await update.message.reply_text(
                    f"🔒 Unauthorized. Your user ID: `{user.id}`\n"
                    f"Set `TELEGRAM_USER_ID={user.id}` in Railway Variables.",
                    parse_mode="Markdown"
                )
                return
            await func(update, context)
        return wrapper

    # ── /start ────────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_start(update, context):
        await send_safe(update.get_bot(), update.effective_chat.id,
            "🤖 *JARVIS* — online και έτοιμος.\n\n"
            "Στείλε μου οτιδήποτε: κείμενο, φωνητικό, φωτογραφία.\n"
            "/help για λίστα εντολών."
        )

    # ── /status ───────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_status(update, context):
        from jarvis.observability.metrics import get_metrics
        from jarvis.memory.database import db_fetch_one
        d = get_metrics().to_dashboard_dict()
        last = await db_fetch_one(
            "SELECT task_type, status, score FROM tasks ORDER BY created_at DESC LIMIT 1"
        )
        last_str = f"{last['task_type']} ({last['status']})" if last else "—"
        h, m = divmod(d["uptime_seconds"] // 60, 60)
        msg = (
            f"*JARVIS Status*\n"
            f"⏱ Uptime: {h}h {m}m\n"
            f"📊 Tasks: {d['tasks']['total']} (failed: {d['tasks']['failed']})\n"
            f"🧠 Memories: {d['memory']['records']}\n"
            f"💰 Cost today: ${d['llm']['cost_usd']:.4f}\n"
            f"📌 Last task: {last_str}"
        )
        await send_safe(update.get_bot(), update.effective_chat.id, msg)

    # ── /clear ────────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_clear(update, context):
        from jarvis.memory.database import db_write
        sid = f"telegram_{update.effective_chat.id}"
        await db_write(
            "UPDATE sessions SET compressed=1 WHERE session_id=?", (sid,)
        )
        await send_safe(update.get_bot(), update.effective_chat.id,
                        "✅ Συνομιλία καθαρίστηκε. Νέα session ξεκινά.")

    # ── /remind ───────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_remind(update, context):
        if not context.args:
            await send_safe(update.get_bot(), update.effective_chat.id,
                "Χρήση: `/remind σε 2 ώρες κάνε κάτι`\n"
                "ή: `/remind in 30 min check server`")
            return

        trigger_ts, reminder_text = _parse_remind_time(context.args)
        if not trigger_ts or not reminder_text:
            await send_safe(update.get_bot(), update.effective_chat.id,
                "❌ Δεν κατάλαβα τον χρόνο. Π.χ. `σε 2 ώρες`, `σε 30 λεπτά`")
            return

        from jarvis.memory.database import db_write
        task_id = str(uuid.uuid4())
        await db_write(
            "INSERT INTO tasks (id, created_at, task_type, payload, priority, status) VALUES (?,?,?,?,?,?)",
            (task_id, trigger_ts, "notification",
             json.dumps({"text": f"⏰ Υπενθύμιση: {reminder_text}"}), 1, "pending"),
        )
        when = time.strftime("%H:%M", time.localtime(trigger_ts))
        await send_safe(update.get_bot(), update.effective_chat.id,
                        f"✅ Υπενθύμιση ορίστηκε για *{when}*: _{reminder_text}_")

    # ── /search ───────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_search(update, context):
        query = " ".join(context.args)
        if not query:
            await send_safe(update.get_bot(), update.effective_chat.id, "Χρήση: `/search <ερώτημα>`")
            return
        await _typing(update.get_bot(), update.effective_chat.id)
        from jarvis.tools.registry import tool_web_search
        result = await tool_web_search(query, max_results=4)
        await send_safe(update.get_bot(), update.effective_chat.id, result[:3000])

    # ── /browse ───────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_browse(update, context):
        url = " ".join(context.args)
        if not url.startswith("http"):
            await send_safe(update.get_bot(), update.effective_chat.id, "Χρήση: `/browse <url>`")
            return
        await _typing(update.get_bot(), update.effective_chat.id)
        from jarvis.tools.registry import tool_web_browse
        text = await tool_web_browse(url)
        await send_safe(update.get_bot(), update.effective_chat.id, text[:3000])

    # ── /run ──────────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_run(update, context):
        code = " ".join(context.args)
        if not code:
            await send_safe(update.get_bot(), update.effective_chat.id,
                "Χρήση: `/run print('hello')`\nΗ στείλε block κώδικα ως κείμενο.")
            return
        await _typing(update.get_bot(), update.effective_chat.id)
        from jarvis.tools.registry import tool_python_exec
        result = await tool_python_exec(code)
        await send_safe(update.get_bot(), update.effective_chat.id, f"```\n{result}\n```")

    # ── /memory ───────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_memory(update, context):
        query = " ".join(context.args) if context.args else ""
        if not query:
            await send_safe(update.get_bot(), update.effective_chat.id, "Χρήση: `/memory <αναζήτηση>`")
            return
        from jarvis.memory.store import search_memories
        results = await search_memories(query, top_k=5)
        if not results:
            await send_safe(update.get_bot(), update.effective_chat.id, "Δεν βρέθηκαν αναμνήσεις.")
            return
        lines = [f"• [{m['time_human']}] {m['content'][:200]}" for m in results]
        await send_safe(update.get_bot(), update.effective_chat.id, "\n\n".join(lines))

    # ── /cost ─────────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_cost(update, context):
        from jarvis.memory.database import db_fetch_all
        rows = await db_fetch_all(
            "SELECT model, SUM(cost_usd) as total FROM api_costs WHERE ts > ? GROUP BY model",
            (time.time() - 86400,),
        )
        if not rows:
            await send_safe(update.get_bot(), update.effective_chat.id,
                           "Μηδενικό κόστος σήμερα (free models).")
            return
        total = sum(r["total"] for r in rows)
        lines = [f"• {r['model']}: ${r['total']:.4f}" for r in rows]
        await send_safe(update.get_bot(), update.effective_chat.id,
                        "*Κόστος σήμερα:*\n" + "\n".join(lines) + f"\n\n*Σύνολο: ${total:.4f}*")

    # ── /logs ─────────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_logs(update, context):
        n = int(context.args[0]) if context.args else 20
        from jarvis.config import LOG_DIR
        log_file = LOG_DIR / "jarvis.log"
        if not log_file.exists():
            await send_safe(update.get_bot(), update.effective_chat.id, "Δεν υπάρχουν logs ακόμα.")
            return
        lines = log_file.read_text().split("\n")
        recent = "\n".join(lines[-n:])
        await send_safe(update.get_bot(), update.effective_chat.id, f"```\n{recent[-3000:]}\n```")

    # ── /history ──────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_history(update, context):
        """Show recent actions from the action_log table."""
        from jarvis.memory.database import db_fetch_all
        n = int(context.args[0]) if context.args else 10
        rows = await db_fetch_all(
            "SELECT tool, input, success, duration_ms, score FROM action_log "
            "ORDER BY ts DESC LIMIT ?",
            (n,),
        )
        if not rows:
            await send_safe(update.get_bot(), update.effective_chat.id, "Δεν υπάρχουν actions ακόμα.")
            return
        lines = []
        for r in rows:
            ok = "✅" if r["success"] else "❌"
            score_str = f" score={r['score']:.0f}" if r.get("score") else ""
            lines.append(f"{ok} `{r['tool']}` {r['input'][:60]!r}{score_str} ({r['duration_ms']:.0f}ms)")
        await send_safe(update.get_bot(), update.effective_chat.id, "\n".join(lines))

    # ── /budget ───────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_budget(update, context):
        """Show current token/cost budget status."""
        from jarvis.llm.client import _daily_cost_usd, _daily_tokens
        from jarvis.config import get_config
        cfg = get_config()
        budget_usd = cfg.llm.daily_token_budget * 0.000004
        pct = (_daily_cost_usd / budget_usd * 100) if budget_usd else 0
        msg = (
            f"*Budget Today*\n"
            f"💰 Spent: ${_daily_cost_usd:.4f} / ${budget_usd:.2f} ({pct:.1f}%)\n"
            f"🔢 Tokens: {_daily_tokens:,}\n"
            f"📊 Limit: {cfg.llm.daily_token_budget:,} tokens/day"
        )
        await send_safe(update.get_bot(), update.effective_chat.id, msg)

    # ── /stop ─────────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_stop(update, context):
        import jarvis.daemon.kairos as _k
        _k._accepting_tasks = False
        await send_safe(update.get_bot(), update.effective_chat.id,
                       "⏸ Autonomous tasks paused. /start για επανεκκίνηση.")

    # ── /skill_proposals ──────────────────────────────────────────────────────

    @auth_required
    async def cmd_skill_proposals(update, context):
        from jarvis.memory.store import get_pending_skill_proposals
        proposals = await get_pending_skill_proposals()
        if not proposals:
            await send_safe(update.get_bot(), update.effective_chat.id, "Δεν υπάρχουν pending proposals.")
            return
        for p in proposals[:5]:
            msg = (f"*Proposal #{p['id']}* για `{p['skill_name']}`:\n"
                   f"```\n{p['proposal'][:500]}\n```\n"
                   f"Accept: POST /skill\\_proposals/{p['id']}/accept")
            await send_safe(update.get_bot(), update.effective_chat.id, msg)

    # ── /help ─────────────────────────────────────────────────────────────────

    @auth_required
    async def cmd_help(update, context):
        await send_safe(update.get_bot(), update.effective_chat.id, """*JARVIS — Εντολές*

💬 *Chat*
Στείλε οτιδήποτε — κείμενο, ερώτηση, εντολή

🎙 *Φωνή*
Στείλε voice message → μεταγράφεται αυτόματα → απαντά

📸 *Φωτογραφία*
Στείλε εικόνα → την αναλύει

⏰ `/remind σε 2 ώρες κάτι` — υπενθύμιση
🔍 `/search <ερώτημα>` — web search
🌐 `/browse <url>` — διάβασε σελίδα
🐍 `/run <κώδικας>` — τρέξε Python
🧠 `/memory <αναζήτηση>` — αναζήτηση μνήμης
📊 `/status` — κατάσταση συστήματος
💰 `/cost` — κόστος API σήμερα
📈 `/budget` — token budget & limits
📋 `/logs` — τελευταία logs
🕐 `/history [n]` — τελευταίες n actions
🗑 `/clear` — καθάρισε session
⏸ `/stop` — παύση background tasks""")

    # ── Voice message handler ─────────────────────────────────────────────────

    @auth_required
    async def handle_voice(update, context):
        """Voice message → Groq Whisper → agent."""
        chat_id = update.effective_chat.id
        await _typing(update.get_bot(), chat_id)

        voice = update.message.voice or update.message.audio
        if not voice:
            return

        try:
            file = await context.bot.get_file(voice.file_id)
            audio_bytes = await file.download_as_bytearray()
            ext = "ogg" if update.message.voice else "mp3"
            text = await _transcribe_groq(bytes(audio_bytes), ext)
        except Exception as e:
            await send_safe(update.get_bot(), chat_id, f"❌ Σφάλμα μεταγραφής: {e}")
            return

        if not text or text.startswith("["):
            await send_safe(update.get_bot(), chat_id, text or "❌ Κενή μεταγραφή.")
            return

        await send_safe(update.get_bot(), chat_id, f"🎙 _{text}_")
        await _typing(update.get_bot(), chat_id)

        session_id = f"telegram_{chat_id}"
        response = await _run_agent(text, session_id)

        # Reply with voice when input was voice (AGI voice-to-voice loop)
        if response and len(response) < 600:
            audio = await _tts_bytes(response)
            if audio:
                try:
                    import io
                    await update.get_bot().send_voice(chat_id, voice=io.BytesIO(audio))
                    await send_safe(update.get_bot(), chat_id, response)  # also send text
                    return
                except Exception:
                    pass

        await send_safe(update.get_bot(), chat_id, response)

    # ── Photo handler ─────────────────────────────────────────────────────────

    @auth_required
    async def handle_photo(update, context):
        """Photo → describe + run agent."""
        chat_id = update.effective_chat.id
        await _typing(update.get_bot(), chat_id)

        caption = update.message.caption or "Περίγραψε αυτή την εικόνα λεπτομερώς."

        # Download highest-res photo
        photo = update.message.photo[-1]
        try:
            file = await context.bot.get_file(photo.file_id)
            photo_bytes = await file.download_as_bytearray()
            # Encode as base64 for vision-capable models
            import base64
            b64 = base64.b64encode(photo_bytes).decode()
            # Build a text prompt describing we have an image
            prompt = (
                f"{caption}\n\n"
                f"[Επισυνάφθηκε εικόνα {photo.width}x{photo.height}px — "
                f"base64 διαθέσιμο αλλά χωρίς vision model χρησιμοποιώ caption: {caption}]"
            )
        except Exception as e:
            prompt = caption

        session_id = f"telegram_{chat_id}"
        response = await _run_agent(prompt, session_id)
        await send_safe(update.get_bot(), chat_id, response)

    # ── Document handler ──────────────────────────────────────────────────────

    @auth_required
    async def handle_document(update, context):
        """Text document → read content → run agent."""
        chat_id = update.effective_chat.id
        await _typing(update.get_bot(), chat_id)

        doc = update.message.document
        caption = update.message.caption or "Ανάλυσε αυτό το αρχείο."

        try:
            file = await context.bot.get_file(doc.file_id)
            content_bytes = await file.download_as_bytearray()
            content = content_bytes.decode("utf-8", errors="replace")[:8000]
            prompt = f"{caption}\n\nΠεριεχόμενο αρχείου `{doc.file_name}`:\n```\n{content}\n```"
        except Exception as e:
            prompt = f"{caption} (αρχείο: {doc.file_name}, σφάλμα ανάγνωσης: {e})"

        session_id = f"telegram_{chat_id}"
        response = await _run_agent(prompt, session_id)
        await send_safe(update.get_bot(), chat_id, response)

    # ── Text message handler ──────────────────────────────────────────────────

    @auth_required
    async def handle_message(update, context):
        """Plain text → full agent with session memory."""
        text = update.message.text
        if not text:
            return

        chat_id = update.effective_chat.id

        try:
            await _typing(update.get_bot(), chat_id)
            session_id = f"telegram_{chat_id}"
            response = await _run_agent(text, session_id)

            # Voice response when user uses trigger words
            voice_trigger = text.lower().startswith(("/voice", "μίλα", "πες μου", "tell me", "speak"))
            if voice_trigger and response and len(response) < 500:
                audio = await _tts_bytes(response)
                if audio:
                    try:
                        import io
                        await update.get_bot().send_voice(chat_id, voice=io.BytesIO(audio))
                        await send_safe(update.get_bot(), chat_id, response)
                        return
                    except Exception:
                        pass

            await send_safe(update.get_bot(), chat_id, response)
        except Exception as e:
            log.error(f"handle_message error: {e}", exc_info=True)
            await send_safe(update.get_bot(), chat_id, f"⚠️ {e}")

    # ── Register all handlers ─────────────────────────────────────────────────

    # ── /urgent — blueprint: Priority 1 task ─────────────────────────────────
    @auth_required
    async def cmd_urgent(update, context):
        task_text = " ".join(context.args) if context.args else ""
        if not task_text:
            await send_safe(update.get_bot(), update.effective_chat.id, "Χρήση: `/urgent <εργασία>`")
            return
        from jarvis.memory.database import db_write
        import uuid as _uuid
        task_id = str(_uuid.uuid4())
        await db_write(
            "INSERT INTO tasks (id, task_type, payload, priority) VALUES (?,?,?,?)",
            (task_id, "simple_qa", json.dumps({"text": task_text}), 1),
        )
        await send_safe(update.get_bot(), update.effective_chat.id, f"🚨 Urgent task queued: _{task_text[:100]}_")

    app.add_handler(CommandHandler("urgent", cmd_urgent))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("browse", cmd_browse))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("skill_proposals", cmd_skill_proposals))
    app.add_handler(CommandHandler("help", cmd_help))

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # ── /ping — no auth, confirms bot is alive ────────────────────────────────

    async def cmd_ping(update, context):
        await update.message.reply_text("🟢 JARVIS online!")

    app.add_handler(CommandHandler("ping", cmd_ping))

    # ── Global error handler — makes errors visible in chat ───────────────────

    async def error_handler(update, context):
        err = str(context.error)
        log.error(f"Telegram error: {err}", exc_info=context.error)
        if update and update.effective_chat:
            try:
                await context.bot.send_message(
                    update.effective_chat.id,
                    f"⚠️ Error: {err[:200]}"
                )
            except Exception:
                pass

    app.add_error_handler(error_handler)

    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    log.info(f"Telegram bot ready (user: {cfg.telegram.allowed_user_id})")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)


# ─── Proactive daily briefing (called by KAIROS) ─────────────────────────────

async def send_daily_briefing():
    """
    Morning briefing sent proactively by KAIROS.
    Includes: pending tasks, recent memories, system status.
    """
    cfg = get_config()
    if not cfg.telegram.enabled:
        return

    from jarvis.observability.metrics import get_metrics
    from jarvis.memory.database import db_fetch_all
    from jarvis.memory.store import search_memories
    import aiohttp

    metrics = get_metrics()
    d = metrics.to_dashboard_dict()

    pending = await db_fetch_all(
        "SELECT task_type, payload FROM tasks WHERE status='pending' ORDER BY priority ASC LIMIT 5"
    )

    recent_memories = await search_memories("σημαντικό", top_k=3)

    lines = [
        "☀️ *Καλημέρα — JARVIS Morning Briefing*\n",
        f"⏱ Uptime: {d['uptime_seconds'] // 3600}h | Tasks: {d['tasks']['total']}",
    ]

    if pending:
        lines.append(f"\n📋 *Pending tasks ({len(pending)}):*")
        for t in pending:
            try:
                payload = json.loads(t["payload"])
                desc = payload.get("text", t["task_type"])[:80]
            except Exception:
                desc = t["task_type"]
            lines.append(f"• {desc}")

    if recent_memories:
        lines.append("\n🧠 *Recent memories:*")
        for m in recent_memories:
            lines.append(f"• {m['content'][:120]}")

    lines.append(f"\n💰 Cost today: ${d['llm']['cost_usd']:.4f}")
    lines.append("\nTipo `/help` για εντολές.")

    msg = "\n".join(lines)
    url = f"https://api.telegram.org/bot{cfg.telegram.bot_token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={
                "chat_id": cfg.telegram.allowed_user_id,
                "text": msg,
                "parse_mode": "Markdown",
            }, timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        log.error(f"Daily briefing failed: {e}")
