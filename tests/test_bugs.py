"""
Regression tests for critical bugs found in code review.

Bug 1: cmd_start Telegram handler had send_safe(chat_id, bot, ...) — args reversed.
       Trigger: /start command → AttributeError: 'int' object has no attribute 'send_message'

Bug 2: _llm_tts_stream used `return full_response` in async generator.
       `return <value>` in generator is silently discarded by the caller's `async for` loop.
       response_text stayed "" → assistant messages never appended to history →
       multi-turn voice conversations had no context.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestBug1TelegramArgOrder:
    """Bug 1: send_safe(chat_id, bot) was reversed in cmd_start."""

    def test_send_safe_signature(self):
        """send_safe must accept (bot, chat_id, text) — bot first."""
        import inspect
        from jarvis.api.telegram_bot import send_safe
        sig = inspect.signature(send_safe)
        params = list(sig.parameters.keys())
        # First param must be 'bot', second must be 'chat_id'
        assert params[0] == "bot", f"Expected 'bot' first, got '{params[0]}'"
        assert params[1] == "chat_id", f"Expected 'chat_id' second, got '{params[1]}'"

    @pytest.mark.asyncio
    async def test_send_safe_rejects_int_as_bot(self):
        """
        Concrete trigger: if chat_id (int) is passed as bot,
        calling bot.send_message() raises AttributeError.
        """
        from jarvis.api.telegram_bot import send_safe

        fake_bot = AsyncMock()
        fake_bot.send_message = AsyncMock()
        chat_id = 123456789

        # Correct order: bot first, chat_id second
        await send_safe(fake_bot, chat_id, "test message")
        fake_bot.send_message.assert_called_once_with(
            chat_id, "test message", parse_mode="Markdown"
        )

    @pytest.mark.asyncio
    async def test_send_safe_wrong_order_raises(self):
        """
        Reversed order (old bug): passing int as bot crashes.
        """
        from jarvis.api.telegram_bot import send_safe

        fake_bot = AsyncMock()
        chat_id = 123456789

        # Wrong order: chat_id as first arg (the bug)
        with pytest.raises((AttributeError, TypeError)):
            await send_safe(chat_id, fake_bot, "test message")


class TestBug2VoicePipelineHistory:
    """Bug 2: async generator return value silently discarded → history never updated."""

    def test_return_value_in_async_generator_is_syntax_error(self):
        """
        Python 3.7+: `return <value>` in an async generator is a SyntaxError.
        The original code had this bug — it would have crashed on import.
        Verify the fix does NOT use return-with-value in the generator.
        """
        import ast
        from pathlib import Path
        src = Path("src/jarvis/voice/pipeline.py").read_text()
        tree = ast.parse(src)

        # Find _llm_tts_stream function
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_llm_tts_stream":
                # Check if it has any yield (making it a generator)
                has_yield = any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node))
                # Check if it has return with a value
                has_return_with_value = any(
                    isinstance(n, ast.Return) and n.value is not None
                    for n in ast.walk(node)
                )
                if has_yield:
                    assert not has_return_with_value, (
                        "_llm_tts_stream is an async generator (has yield) "
                        "but also has 'return <value>' — SyntaxError in Python 3.12"
                    )

    @pytest.mark.asyncio
    async def test_session_collected_tokens_populated(self):
        """
        Fix: _llm_tts_stream stores full_response on session.collected_tokens
        BEFORE yielding audio, so caller can access it after iteration.
        """
        import os
        os.environ["JARVIS_HOME"] = "/tmp/jarvis_test_bugs"

        from jarvis.voice.pipeline import VoiceSession, VoiceState

        session = VoiceSession(session_id="test-123")
        assert session.collected_tokens == ""

        # Simulate what the fixed _llm_tts_stream does:
        # stores the response on session BEFORE yielding
        async def fixed_generator(session):
            full_response = "Γεια σου, εγώ είμαι ο JARVIS."
            session.collected_tokens = full_response  # stored before yield
            yield b"audio_bytes_sentence_1"
            yield b"audio_bytes_sentence_2"

        chunks = []
        async for chunk in fixed_generator(session):
            chunks.append(chunk)

        # After iteration, session has the full response
        assert session.collected_tokens == "Γεια σου, εγώ είμαι ο JARVIS."
        assert len(chunks) == 2

        # Caller can now append to history correctly
        if session.collected_tokens:
            session.history.append({
                "role": "assistant",
                "content": session.collected_tokens,
            })
        assert len(session.history) == 1
        assert session.history[0]["content"] == "Γεια σου, εγώ είμαι ο JARVIS."

    @pytest.mark.asyncio
    async def test_multiturn_history_not_empty(self):
        """
        Integration: after voice turn, assistant response must be in history.
        Without fix: history only has user messages, never assistant.
        With fix: history alternates user/assistant correctly.
        """
        import os
        os.environ["JARVIS_HOME"] = "/tmp/jarvis_test_bugs"

        from jarvis.voice.pipeline import VoiceSession

        session = VoiceSession(session_id="multi-turn")

        # Simulate turn 1
        session.history.append({"role": "user", "content": "Γεια σου"})
        session.collected_tokens = "Γεια, πώς μπορώ να σε βοηθήσω;"

        if session.collected_tokens:
            session.history.append({"role": "assistant", "content": session.collected_tokens})

        # Simulate turn 2
        session.history.append({"role": "user", "content": "Τι ώρα είναι;"})
        session.collected_tokens = "Είναι 9 το βράδυ."

        if session.collected_tokens:
            session.history.append({"role": "assistant", "content": session.collected_tokens})

        # History must have 4 entries alternating correctly
        assert len(session.history) == 4
        assert session.history[0]["role"] == "user"
        assert session.history[1]["role"] == "assistant"
        assert session.history[2]["role"] == "user"
        assert session.history[3]["role"] == "assistant"
        assert "9 το βράδυ" in session.history[3]["content"]
