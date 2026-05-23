"""Re-export Telegram bot functions from core.telegram_bot."""


async def send_safe(bot, chat_id: int, text: str, parse_mode: str = "Markdown"):
    """Send a message safely with proper argument order.

    Args:
        bot: Telegram bot instance (must have send_message method)
        chat_id: Chat ID to send to
        text: Message text
        parse_mode: Parse mode (default: Markdown)
    """
    await bot.send_message(chat_id, text, parse_mode=parse_mode)


__all__ = ['send_safe']
