"""Qdrant MCP server — vector database for semantic memory.

Provides CRUD tools over Qdrant collections backed by 384-dim vectors
(sentence-transformers MiniLM-L12-v2). One collection per memory domain:
episodes, facts, entities, rules.

Tools:
    qdrant_create_collection  — create a named collection
    qdrant_upsert             — insert or update vectors
    qdrant_search             — cosine-similarity ANN search
    qdrant_delete             — delete vectors by ID or filter

Environment:
    QDRANT_URL        — Qdrant endpoint (default: http://localhost:6333)
    QDRANT_API_KEY    — optional API key for Qdrant Cloud
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from ._common import Server, Tool, require, require_str, require_in, require_int, require_dict, ok, err

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
EMBED_DIM = 384

# ─── Lazy client singleton ──────────────────────────────────────────────────
_qdrant: Any = None
_qdrant_models: Any = None


def _get_client():
    """Lazy-init Qdrant client. Reuses connection across calls."""
    global _qdrant, _qdrant_models
    if _qdrant is None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client import models as qm
        except ImportError:
            raise RuntimeError(
                "qdrant-client not installed; pip install qdrant-client"
            )
        _qdrant_models = qm
        client_kwargs: dict = {"url": QDRANT_URL}
        if QDRANT_API_KEY:
            client_kwargs["api_key"] = QDRANT_API_KEY
        _qdrant = QdrantClient(**client_kwargs)
    return _qdrant, _qdrant_models


def _validate_vectors(vectors: list[list[float]]) -> None:
    """Ensure all vectors match EMBED_DIM."""
    for i, v in enumerate(vectors):
        if not isinstance(v, list) or len(v) != EMBED_DIM:
            raise ValueError(
                f"vector[{i}] must be list of {EMBED_DIM} floats, got "
                f"{type(v).__name__} len={len(v) if isinstance(v, list) else '?'}"
            )


# ─── Tool implementations ───────────────────────────────────────────────────


async def _tool_create_collection(args: dict) -> dict:
    name = require_str(args, "name")
    require(len(name) <= 64, "collection name must be <= 64 chars")
    vectors_config = args.get("vectors_config", {})
    size = vectors_config.get("size", EMBED_DIM)
    distance = vectors_config.get("distance", "Cosine")

    client, qm = _get_client()
    existing = [c.name for c in client.get_collections().collections]
    if name in existing:
        return ok({"collection": name, "status": "already_exists"})

    client.create_collection(
        collection_name=name,
        vectors_config=qm.VectorParams(size=size, distance=distance),
    )
    return ok({"collection": name, "status": "created", "dim": size})


async def _tool_upsert(args: dict) -> dict:
    collection = require_str(args, "collection")
    require_dict(args, "points")
    points_raw: dict[str, list[float]] = args["points"]   # id -> vector map
    payload_base: dict = args.get("payload_base", {})

    client, qm = _get_client()
    points = []
    for pid, vector in points_raw.items():
        if not isinstance(vector, list) or len(vector) != EMBED_DIM:
            return err(
                f"vector for point {pid!r} must be list of {EMBED_DIM} floats",
                code="invalid_vector",
            )
        points.append(
            qm.PointStruct(
                id=str(pid),
                vector=vector,
                payload=payload_base | {"id": pid},
            )
        )

    client.upsert(collection_name=collection, points=points)
    return ok({"collection": collection, "upserted": len(points)})


async def _tool_search(args: dict) -> dict:
    collection = require_str(args, "collection")
    require(args.get("vector") is not None, "vector required for search")
    vector = args["vector"]
    if not isinstance(vector, list) or len(vector) != EMBED_DIM:
        return err(
            f"vector must be list of {EMBED_DIM} floats",
            code="invalid_vector",
        )
    limit = require_int(args, "limit", min_val=1, max_val=100, default=10)
    score_threshold = float(args.get("score_threshold", 0.5))

    client, _ = _get_client()
    results = client.search(
        collection_name=collection,
        query_vector=vector,
        limit=limit,
        score_threshold=score_threshold,
        with_payload=True,
    )
    return ok(
        {
            "collection": collection,
            "results": [
                {
                    "id": r.id,
                    "score": round(r.score, 4),
                    "payload": r.payload,
                }
                for r in results
            ],
        }
    )


async def _tool_delete(args: dict) -> dict:
    collection = require_str(args, "collection")
    ids = args.get("ids")
    filter_expr = args.get("filter")

    if not ids and not filter_expr:
        return err("one of 'ids' or 'filter' required", code="invalid_input")

    client, qm = _get_client()

    if ids:
        if not isinstance(ids, list):
            return err("'ids' must be a list", code="invalid_input")
        ids_str = [str(i) for i in ids]
        result = client.delete(collection_name=collection, points_selector=ids_str)
        return ok({"collection": collection, "deleted": len(ids_str)})

    # Filter-based delete
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    filter_obj = Filter(
        must=[
            FieldCondition(key=k, match=MatchValue(value=v))
            for k, v in filter_expr.items()
        ]
    )
    result = client.delete(
        collection_name=collection,
        points_selector=qm.FilterSelector(filter=filter_obj),
    )
    return ok({"collection": collection, "deleted": "filter_applied", "filter": filter_expr})


# ─── Server factory ─────────────────────────────────────────────────────────


def get_server() -> Server:
    srv = Server(name="qdrant")

    @srv.tool(
        name="qdrant_create_collection",
        description="Create a Qdrant collection for storing embeddings",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Collection name (max 64 chars)"},
                "vectors_config": {
                    "type": "object",
                    "properties": {
                        "size": {"type": "integer", "description": "Vector dimension (default 384)"},
                        "distance": {"type": "string", "enum": ["Cosine", "Euclid", "Dot"], "description": "Distance metric"},
                    },
                },
            },
            "required": ["name"],
        },
    )
    async def _create_collection(args):
        return await _tool_create_collection(args)

    @srv.tool(
        name="qdrant_upsert",
        description="Insert or update vectors in a Qdrant collection",
        input_schema={
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "points": {"type": "object", "description": "Map of id → vector([384 floats])"},
                "payload_base": {"type": "object", "description": "Base payload merged into every point"},
            },
            "required": ["collection", "points"],
        },
    )
    async def _upsert(args):
        return await _tool_upsert(args)

    @srv.tool(
        name="qdrant_search",
        description="ANN search in a Qdrant collection by cosine similarity",
        input_schema={
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "vector": {"type": "array", "items": {"type": "number"}, "description": "384-dim query vector"},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
                "score_threshold": {"type": "number", "default": 0.5, "minimum": 0, "maximum": 1},
            },
            "required": ["collection", "vector"],
        },
    )
    async def _search(args):
        return await _tool_search(args)

    @srv.tool(
        name="qdrant_delete",
        description="Delete vectors from a Qdrant collection by ID or filter",
        input_schema={
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "string"}, "description": "Vector IDs to delete"},
                "filter": {"type": "object", "description": "Filter expression (field→value map)"},
            },
            "required": ["collection"],
        },
    )
    async def _delete(args):
        return await _tool_delete(args)

    return srv
