"""
Embedding generation — 100% FREE using sentence-transformers (local).
Optional: Ollama nomic-embed-text (also free, local).
Optional: OpenAI text-embedding-3-small (paid, higher quality).

v6 fixes:
- Single embedding model for ALL vectors (never mix dimensions)
- Model loaded ONCE at startup, not per-call
- CPU-bound work offloaded to ThreadPoolExecutor
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np

from jarvis.config import get_config
from jarvis.observability.logger import get_logger

log = get_logger("embeddings")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="embed")
_model: Any = None
_model_name: str = ""
_embed_dim: int = 384


def _load_model():
    """Load embedding model once (called from executor)."""
    global _model, _model_name, _embed_dim
    cfg = get_config()
    provider = cfg.memory.embed_provider

    if provider == "sentence-transformers":
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(cfg.memory.embed_model)
        _embed_dim = cfg.memory.embed_dim
        _model_name = cfg.memory.embed_model
        log.info(f"Loaded sentence-transformers model: {_model_name} ({_embed_dim}d)")

    elif provider == "ollama":
        import httpx
        _model = cfg.llm.ollama_embed_model
        _model_name = _model
        # Determine dim via test embedding
        resp = httpx.post(
            f"{cfg.llm.ollama_base_url}/api/embeddings",
            json={"model": _model, "prompt": "test"},
            timeout=30,
        )
        _embed_dim = len(resp.json()["embedding"])
        log.info(f"Loaded Ollama embedding model: {_model_name} ({_embed_dim}d)")

    elif provider == "openai":
        import openai
        # Use the *sync* client — _embed_sync runs inside a ThreadPoolExecutor,
        # which cannot drive an asyncio event loop. AsyncOpenAI would silently
        # return before the coroutine completes, producing all-zero vectors.
        _model = openai.OpenAI(api_key=cfg.llm.openai_api_key)
        _model_name = "text-embedding-3-small"
        _embed_dim = 1536
        log.info(f"Using OpenAI embeddings: {_model_name}")

    elif provider == "jina":
        import os
        # Jina AI: free 1M tokens/month — set JINA_API_KEY in Railway Variables
        # No heavy dependencies, pure HTTP call from executor thread
        _model = os.environ.get("JINA_API_KEY", "")
        _model_name = "jina-embeddings-v3"
        _embed_dim = 1024
        log.info(f"Using Jina AI embeddings: {_model_name} ({_embed_dim}d)")

    else:
        raise ValueError(f"Unknown embed_provider: {provider}")


async def ensure_model_loaded():
    """Load model in executor so it doesn't block the event loop."""
    global _model
    if _model is None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, _load_model)


def _embed_sync(texts: list[str]) -> list[list[float]]:
    """Synchronous embedding — runs in executor."""
    cfg = get_config()
    provider = cfg.memory.embed_provider

    if provider == "sentence-transformers":
        embeddings = _model.encode(texts, normalize_embeddings=True)
        return embeddings.tolist()

    elif provider == "ollama":
        import httpx
        results = []
        for text in texts:
            resp = httpx.post(
                f"{cfg.llm.ollama_base_url}/api/embeddings",
                json={"model": _model, "prompt": text},
                timeout=30,
            )
            results.append(resp.json()["embedding"])
        return results

    elif provider == "openai":
        # _model is openai.OpenAI (sync client) — safe to call from executor thread
        response = _model.embeddings.create(input=texts, model=_model_name)
        return [item.embedding for item in response.data]

    elif provider == "jina":
        import httpx
        api_key = _model  # stored as API key string during _load_model
        resp = httpx.post(
            "https://api.jina.ai/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"input": texts, "model": _model_name},
            timeout=10,  # fail fast — fallback to FTS if slow
        )
        resp.raise_for_status()
        data = resp.json()
        return [item["embedding"] for item in data["data"]]

    raise ValueError(f"_embed_sync: unhandled provider '{provider}'")


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    Async embedding — offloads to executor.
    Returns list of float vectors, one per text.
    """
    await ensure_model_loaded()
    if not texts:
        return []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _embed_sync, texts)


async def embed_text(text: str) -> list[float]:
    """Embed a single text."""
    results = await embed_texts([text])
    return results[0]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    norm_a = np.linalg.norm(va)
    norm_b = np.linalg.norm(vb)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(va, vb) / (norm_a * norm_b))


def pack_embedding(vec: list[float]) -> bytes:
    """Pack float list to bytes for SQLite storage."""
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_embedding(blob: bytes) -> list[float]:
    """Unpack bytes from SQLite to float list."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))
