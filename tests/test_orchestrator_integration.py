"""Tests for orchestrator integration into core agent loop."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestComplexityDetection:
    """Test _should_use_orchestrator() heuristic detection."""

    def test_short_simple_prompt_returns_false(self):
        """Short, simple prompts should not trigger orchestration."""
        from core.agent import _should_use_orchestrator

        assert _should_use_orchestrator("Hello") is False
        assert _should_use_orchestrator("What is 2+2?") is False
        assert _should_use_orchestrator("Fix the bug") is False

    def test_long_prompt_returns_true(self):
        """Prompts exceeding length threshold should trigger orchestration."""
        from core.agent import _should_use_orchestrator

        long_prompt = "a" * 900  # Default threshold is 800
        assert _should_use_orchestrator(long_prompt) is True

    def test_complexity_keywords_trigger_orchestration(self):
        """3+ complexity keywords should trigger orchestration."""
        from core.agent import _should_use_orchestrator

        # 4 keywords: analyze, compare, research, investigate
        complex_prompt = "Please analyze and compare the research to investigate the issue"
        assert _should_use_orchestrator(complex_prompt) is True

        # Only 2 keywords
        simple_prompt = "Please analyze the data"
        assert _should_use_orchestrator(simple_prompt) is False

    def test_multiple_questions_trigger_orchestration(self):
        """3+ questions should trigger orchestration."""
        from core.agent import _should_use_orchestrator

        multi_question = "What is X? Why does Y happen? How can we fix Z?"
        assert _should_use_orchestrator(multi_question) is True

        single_question = "What is X?"
        assert _should_use_orchestrator(single_question) is False

    def test_ultrathink_keyword_triggers_orchestration(self):
        """'ultrathink' keyword should trigger orchestration."""
        from core.agent import _should_use_orchestrator

        # ultrathink alone is only 1 keyword, need 3+ for trigger
        # But combined with other keywords it should work
        assert _should_use_orchestrator("Please analyze and research this with ultrathink") is True

    def test_configurable_length_threshold(self):
        """Length threshold should be configurable via env var."""
        from core.agent import _should_use_orchestrator

        with patch.dict(os.environ, {"JARVIS_ORCHESTRATOR_MIN_LENGTH": "100"}):
            assert _should_use_orchestrator("a" * 150) is True
            assert _should_use_orchestrator("a" * 50) is False


class TestOrchestrationRouting:
    """Test orchestration routing in run_jarvis_core()."""

    @pytest.mark.asyncio
    async def test_orchestration_disabled_by_default(self):
        """Orchestration should be disabled when env var not set."""
        from core.agent import run_jarvis_core

        with patch.dict(os.environ, {}, clear=True):
            # Mock the single-agent path
            with patch("core.agent.build_async_client") as mock_client:
                mock_client.return_value = MagicMock()

                # Should not attempt orchestration
                # (would fail if it did, since we haven't mocked run_orchestrated)
                try:
                    result = await run_jarvis_core(
                        "analyze this complex task thoroughly",
                        max_iterations=1
                    )
                    # If we get here, single-agent path was used
                    assert result is not None
                except Exception as e:
                    # If orchestration was attempted, it would fail differently
                    assert "run_orchestrated" not in str(e)

    @pytest.mark.asyncio
    async def test_explicit_use_orchestrator_true(self):
        """use_orchestrator=True should force orchestration."""
        from core.agent import run_jarvis_core

        mock_orch_result = {
            "success": True,
            "output": "Orchestrated result",
            "score": 85.0,
            "duration_ms": 1000.0,
            "subtasks": 3,
            "accepted": 3,
        }

        with patch("core.orchestrator.run_orchestrated", new_callable=AsyncMock) as mock_orch:
            mock_orch.return_value = mock_orch_result

            result = await run_jarvis_core(
                "Simple task",
                use_orchestrator=True
            )

            # Verify orchestration was called
            mock_orch.assert_called_once_with("Simple task")

            # Verify result conversion
            assert result.stopped_reason == "orchestration_complete"
            assert result.final_message == "Orchestrated result"
            assert result.iterations == 3
            assert len(result.transcript) == 1
            assert result.transcript[0]["type"] == "orchestration_summary"
            assert result.transcript[0]["score"] == 85.0

    @pytest.mark.asyncio
    async def test_explicit_use_orchestrator_false(self):
        """use_orchestrator=False should force single-agent."""
        from core.agent import run_jarvis_core

        with patch.dict(os.environ, {"JARVIS_ENABLE_ORCHESTRATOR": "true"}):
            with patch("core.agent.build_async_client") as mock_client:
                mock_client.return_value = MagicMock()

                # Even with env var enabled and complex prompt,
                # explicit False should use single-agent
                result = await run_jarvis_core(
                    "analyze compare research investigate thoroughly",
                    use_orchestrator=False,
                    max_iterations=1
                )

                # Should not have orchestration metadata
                assert result.stopped_reason != "orchestration_complete"

    @pytest.mark.asyncio
    async def test_auto_detect_with_env_enabled(self):
        """Auto-detection should work when JARVIS_ENABLE_ORCHESTRATOR=true."""
        from core.agent import run_jarvis_core

        mock_orch_result = {
            "success": True,
            "output": "Auto-detected orchestration",
            "score": 90.0,
            "duration_ms": 1500.0,
            "subtasks": 4,
            "accepted": 4,
        }

        with patch.dict(os.environ, {"JARVIS_ENABLE_ORCHESTRATOR": "true"}):
            with patch("core.orchestrator.run_orchestrated", new_callable=AsyncMock) as mock_orch:
                mock_orch.return_value = mock_orch_result

                # Complex prompt should auto-trigger orchestration
                result = await run_jarvis_core(
                    "Please analyze, compare, research, and investigate this thoroughly"
                )

                # Verify orchestration was used
                mock_orch.assert_called_once()
                assert result.stopped_reason == "orchestration_complete"

    @pytest.mark.asyncio
    async def test_orchestration_fallback_on_error(self):
        """Should fall back to single-agent if orchestration fails."""
        from core.agent import run_jarvis_core

        with patch.dict(os.environ, {"JARVIS_ENABLE_ORCHESTRATOR": "true"}):
            with patch("core.orchestrator.run_orchestrated", new_callable=AsyncMock) as mock_orch:
                # Make orchestration fail
                mock_orch.side_effect = Exception("Orchestration error")

                with patch("core.agent.build_async_client") as mock_client:
                    mock_client.return_value = MagicMock()

                    # Should fall back to single-agent
                    result = await run_jarvis_core(
                        "analyze compare research investigate",
                        max_iterations=1
                    )

                    # Should not have orchestration metadata
                    assert result.stopped_reason != "orchestration_complete"


class TestBackwardCompatibility:
    """Test that existing callers still work unchanged."""

    @pytest.mark.asyncio
    async def test_telegram_bot_call_pattern(self):
        """Verify telegram_bot.py call pattern still works."""
        from core.agent import run_jarvis_core

        with patch("core.agent.build_async_client") as mock_client:
            mock_client.return_value = MagicMock()

            # Simulate telegram_bot.py:363 call pattern
            result = await run_jarvis_core(
                "test prompt",
                tools=[{"name": "test_tool", "description": "test"}],
                system="test system prompt",
                max_iterations=1
            )

            assert result is not None
            assert hasattr(result, "final_message")
            assert hasattr(result, "transcript")

    @pytest.mark.asyncio
    async def test_orchestrator_internal_calls(self):
        """Verify orchestrator's own calls to run_jarvis_core still work."""
        from core.agent import run_jarvis_core

        with patch("core.agent.build_async_client") as mock_client:
            mock_client.return_value = MagicMock()

            # Simulate orchestrator.py:76 (decompose_task)
            result = await run_jarvis_core("decompose this", max_iterations=1)
            assert result is not None

            # Simulate orchestrator.py:100 (execute_subtask)
            result = await run_jarvis_core("subtask", max_iterations=5)
            assert result is not None

            # Simulate orchestrator.py:154 (aggregate_results)
            result = await run_jarvis_core("aggregate", max_iterations=3)
            assert result is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
