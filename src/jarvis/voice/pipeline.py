"""Voice pipeline module - provides VoiceSession for tests."""


class VoiceSession:
    """Voice session that maintains conversation history and state."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.history = []
        self.collected_tokens = ""

    def add_user_message(self, content: str):
        """Add a user message to history."""
        self.history.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str):
        """Add an assistant message to history."""
        self.history.append({"role": "assistant", "content": content})


__all__ = ['VoiceSession']
