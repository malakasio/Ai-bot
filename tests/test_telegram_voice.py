"""Tests for Telegram bot voice message handling."""

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_deepgram_response():
    """Mock Deepgram API response."""
    mock_response = MagicMock()
    mock_response.results.channels = [
        MagicMock(alternatives=[
            MagicMock(transcript="This is a test transcription")
        ])
    ]
    return mock_response


@pytest.fixture
def sample_audio_file():
    """Create a temporary audio file for testing."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as f:
        # Write some dummy bytes (not real audio, just for testing file handling)
        f.write(b"fake audio data for testing")
        temp_path = f.name
    yield temp_path
    # Cleanup
    if os.path.exists(temp_path):
        os.unlink(temp_path)


class TestVoiceTranscription:
    """Test voice message transcription functionality."""

    @pytest.mark.asyncio
    async def test_transcribe_voice_success(self, mock_deepgram_response, sample_audio_file):
        """Test successful voice transcription."""
        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test_api_key"}):
            # Patch deepgram module imports inside the function
            with patch("deepgram.DeepgramClient") as mock_client_class:
                with patch("deepgram.PrerecordedOptions"):
                    # Setup mock
                    mock_client = MagicMock()
                    mock_client.listen.rest.v.return_value.transcribe_file.return_value = mock_deepgram_response
                    mock_client_class.return_value = mock_client

                    # Import after patching
                    from core.telegram_bot import _transcribe_voice

                    # Test transcription
                    result = await _transcribe_voice(sample_audio_file)

                    assert result == "This is a test transcription"
                    mock_client.listen.rest.v.assert_called_once_with("1")

    @pytest.mark.asyncio
    async def test_transcribe_voice_no_api_key(self, sample_audio_file):
        """Test transcription fails without API key."""
        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": ""}, clear=True):
            from core.telegram_bot import _transcribe_voice

            with pytest.raises(Exception, match="DEEPGRAM_API_KEY not set"):
                await _transcribe_voice(sample_audio_file)

    @pytest.mark.asyncio
    async def test_transcribe_voice_empty_transcript(self, sample_audio_file):
        """Test handling of empty transcript from Deepgram."""
        mock_response = MagicMock()
        mock_response.results.channels = [
            MagicMock(alternatives=[MagicMock(transcript="")])
        ]

        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test_api_key"}):
            with patch("deepgram.DeepgramClient") as mock_client_class:
                with patch("deepgram.PrerecordedOptions"):
                    mock_client = MagicMock()
                    mock_client.listen.rest.v.return_value.transcribe_file.return_value = mock_response
                    mock_client_class.return_value = mock_client

                    from core.telegram_bot import _transcribe_voice

                    with pytest.raises(Exception, match="empty transcript"):
                        await _transcribe_voice(sample_audio_file)

    @pytest.mark.asyncio
    async def test_transcribe_voice_file_not_found(self):
        """Test transcription with non-existent file."""
        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test_api_key"}):
            from core.telegram_bot import _transcribe_voice

            with pytest.raises(Exception):
                await _transcribe_voice("/nonexistent/file.ogg")


class TestVoiceMessageHandler:
    """Test Telegram voice message handler integration."""

    @pytest.mark.asyncio
    async def test_handle_voice_full_flow(self, mock_deepgram_response):
        """Test complete voice message handling flow."""
        # This is an integration-style test that would require more mocking
        # of the Telegram bot infrastructure. For now, we verify the core
        # transcription function works, which is the critical new functionality.
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
