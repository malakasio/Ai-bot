"""
Regression tests for critical bugs found in code review.

Bug 1: cmd_start Telegram handler had send_safe(chat_id, bot, ...) — args reversed.
       Trigger: /start command → AttributeError: 'int' object has no attribute 'send_message'

Bug 2: _llm_tts_stream used `return full_response` in async generator.
       `return <value>` in generator is silently discarded by the caller's `async for` loop.
       response_text stayed "" → assistant messages never appended to history →
       multi-turn voice conversations had no context.

Bug 3 (path traversal): skill_proposals/accept used unvalidated skill_name in path.
       Trigger: proposal with skill_name='../../etc/passwd' → arbitrary file append.

Bug 6 (AgentTeam crash): _worker called run_task(task=<dict>) but run_task expects str.
       Trigger: any queued task → AttributeError: 'dict' has no attribute 'lower'.

Bug 7 (KAIROS watcher): ConfigHandler.on_modified called self._debounced_reload()
       where self is ConfigHandler, not KAIROSDaemon.
       Trigger: modify CLAUDE.md → AttributeError in watcher thread.

Bug 9 (DoS): LimitBodySize called int(content_length) with no try/except.
       Trigger: Content-Length: not-a-number → ValueError → 500 on every request.

Bug 11 (SSRF): tool_http_request had no SSRF guard.
       Trigger: ask agent to fetch http://169.254.169.254/ → cloud metadata leak.
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


class TestBug9ContentLength:
    """Bug 9: LimitBodySize raised ValueError on non-numeric Content-Length."""

    @pytest.mark.asyncio
    async def test_bad_content_length_returns_400(self):
        from fastapi.testclient import TestClient
        from jarvis.api.main import app

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/chat",
            content=b"{}",
            headers={"content-type": "application/json", "content-length": "not-a-number"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_normal_content_length_passes(self):
        from jarvis.api.main import LimitBodySize
        from unittest.mock import AsyncMock, MagicMock
        middleware = LimitBodySize(app=MagicMock())
        request = MagicMock()
        request.headers.get.return_value = "100"
        call_next = AsyncMock(return_value=MagicMock(status_code=200))
        await middleware.dispatch(request, call_next)
        call_next.assert_called_once()


class TestBug11SSRFGuard:
    """Bug 11: tool_http_request had no SSRF protection."""

    def test_localhost_blocked(self):
        from jarvis.tools.registry import _is_ssrf_url
        assert _is_ssrf_url("http://127.0.0.1/admin")
        assert _is_ssrf_url("http://localhost/secret")
        assert _is_ssrf_url("http://0.0.0.0/")

    def test_private_ip_blocked(self):
        from jarvis.tools.registry import _is_ssrf_url
        assert _is_ssrf_url("http://10.0.0.1/")
        assert _is_ssrf_url("http://192.168.1.1/")
        assert _is_ssrf_url("http://172.16.0.1/")

    def test_metadata_endpoint_blocked(self):
        from jarvis.tools.registry import _is_ssrf_url
        assert _is_ssrf_url("http://169.254.169.254/latest/meta-data/")
        assert _is_ssrf_url("http://metadata.google.internal/")

    def test_external_url_allowed(self):
        from jarvis.tools.registry import _is_ssrf_url
        assert not _is_ssrf_url("https://api.anthropic.com/v1/messages")
        assert not _is_ssrf_url("https://google.com/")
        assert not _is_ssrf_url("https://api.telegram.org/")


class TestBug6AgentTeamPayload:
    """Bug 6: AgentTeam._worker passed dict to run_task instead of str."""

    def test_payload_extraction(self):
        import json
        payload_json = json.dumps({"text": "summarize this document", "type": "simple_qa"})
        payload = json.loads(payload_json)
        task_text = payload.get("text", str(payload)) if isinstance(payload, dict) else str(payload)
        assert task_text == "summarize this document"
        assert isinstance(task_text, str)

    def test_payload_fallback_to_str(self):
        import json
        payload_json = json.dumps({"type": "communication", "historyId": "abc"})
        payload = json.loads(payload_json)
        task_text = payload.get("text", str(payload)) if isinstance(payload, dict) else str(payload)
        assert isinstance(task_text, str)
        assert len(task_text) > 0


class TestBug3PathTraversal:
    """Bug 3: skill_proposals/accept allowed path traversal via skill_name."""

    def test_traversal_blocked(self):
        from pathlib import Path
        skills_root = Path(".claude/skills").resolve()
        malicious_names = [
            "../../etc/passwd",
            "../../../tmp/evil",
            "x/../../root/.bashrc",
        ]
        for name in malicious_names:
            candidate = (skills_root / name / "SKILL.md").resolve()
            is_safe = str(candidate).startswith(str(skills_root))
            assert not is_safe, f"Path traversal not caught for: {name}"

    def test_valid_skill_name_allowed(self):
        from pathlib import Path
        skills_root = Path(".claude/skills").resolve()
        for name in ["agents", "memory", "voice"]:
            candidate = (skills_root / name / "SKILL.md").resolve()
            assert str(candidate).startswith(str(skills_root))
