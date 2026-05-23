"""Memory embeddings module - re-exports from core.embeddings."""

from core.embeddings import embed_text


def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    import math

    if len(vec1) != len(vec2):
        raise ValueError("Vectors must have same length")

    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return dot_product / (norm1 * norm2)


__all__ = ['embed_text', 'cosine_similarity']
