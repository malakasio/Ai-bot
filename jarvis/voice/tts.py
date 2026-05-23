"""TTS module - text-to-speech functions."""

import re


def _split_sentences_sync(text: str) -> list[str]:
    """Split text into sentences, handling special cases like IPs."""
    # Protect IP addresses from being split
    ip_pattern = r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'

    # Find all IPs and replace with placeholders
    ips = re.findall(ip_pattern, text)
    protected_text = text
    ip_placeholders = {}

    for i, ip in enumerate(ips):
        placeholder = f"__IP{i}__"
        ip_placeholders[placeholder] = ip
        protected_text = protected_text.replace(ip, placeholder)

    # Split on sentence boundaries
    sentences = re.split(r'(?<=[.!?])\s+', protected_text)

    # Restore IPs
    result = []
    for sentence in sentences:
        for placeholder, ip in ip_placeholders.items():
            sentence = sentence.replace(placeholder, ip)
        result.append(sentence)

    return [s for s in result if s.strip()]


async def synthesize_full(text: str) -> bytes:
    """Synthesize speech from text using edge-tts."""
    try:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice="el-GR-NestorasNeural")
        audio_chunks = []

        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_chunks.append(chunk["data"])

        return b"".join(audio_chunks)

    except ImportError:
        # edge-tts not installed
        return b""
    except Exception:
        # Network error or other issue
        return b""


__all__ = ['_split_sentences_sync', 'synthesize_full']
