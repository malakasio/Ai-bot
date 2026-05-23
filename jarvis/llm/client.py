"""LLM client module with circuit breaker."""

import hashlib
import json
from collections import defaultdict


class CircuitBreaker:
    """Circuit breaker to prevent infinite tool loops."""

    def __init__(self, max_same: int = 3):
        """Initialize circuit breaker.

        Args:
            max_same: Maximum number of identical tool calls allowed
        """
        self.max_same = max_same
        self.call_history = defaultdict(int)

    def _hash_call(self, tool_name: str, args: dict) -> str:
        """Create a hash of the tool call for tracking."""
        # Sort args for consistent hashing
        args_str = json.dumps(args, sort_keys=True)
        call_str = f"{tool_name}:{args_str}"
        return hashlib.sha256(call_str.encode()).hexdigest()

    def check(self, tool_name: str, args: dict) -> bool:
        """Check if a tool call should be allowed.

        Returns:
            True if call is allowed, False if blocked
        """
        call_hash = self._hash_call(tool_name, args)
        self.call_history[call_hash] += 1

        if self.call_history[call_hash] > self.max_same:
            return False

        return True

    def reset(self):
        """Reset the circuit breaker."""
        self.call_history.clear()


__all__ = ['CircuitBreaker']
