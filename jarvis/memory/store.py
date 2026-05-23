"""Memory store module - context management functions."""


def trim_context(messages: list[dict], system: str, max_tokens: int = 4000) -> list[dict]:
    """Trim context to fit within token limit, preserving message pairs.

    Removes oldest user/assistant pairs first, ensuring we don't have
    orphaned assistant messages at the start.
    """
    # Simple token estimation: ~4 chars per token
    def estimate_tokens(text: str) -> int:
        return len(text) // 4

    system_tokens = estimate_tokens(system)
    available_tokens = max_tokens - system_tokens

    if available_tokens <= 0:
        return []

    # Calculate tokens for each message
    message_tokens = [estimate_tokens(msg.get('content', '')) for msg in messages]
    total_tokens = sum(message_tokens)

    if total_tokens <= available_tokens:
        return messages

    # Remove pairs from the beginning
    result = messages[:]

    while len(result) >= 2 and sum(estimate_tokens(m.get('content', '')) for m in result) > available_tokens:
        # Remove first pair (user + assistant)
        if len(result) >= 2:
            # Ensure we're removing a user message first
            if result[0]['role'] == 'user':
                result = result[2:]  # Remove user + assistant pair
            else:
                result = result[1:]  # Remove orphaned assistant message
        else:
            break

    # Ensure we start with a user message
    while result and result[0]['role'] != 'user':
        result = result[1:]

    return result


__all__ = ['trim_context']
