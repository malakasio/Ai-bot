Skill: Memory Management

Purpose

Persist, retrieve, and consolidate information across sessions using a 4-level hierarchy.

Layer Descriptions

L1: Working Memory (in-context)

Storage: Python dict in current process
Capacity: ~100k tokens (Claude context window)
Retention: Current session only
Access time: <1ms
L2: Episodic Memory (PostgreSQL hypertables)

Storage: TimescaleDB hypertables
Capacity: Unlimited (time-partitioned)
Retention: 90 days raw, consolidated forever
Access time: 10–50ms
Schema: (id, timestamp, session_id, agent, action, input, output, score, tags)
L3: Semantic Memory (pgvector)

Storage: PostgreSQL + pgvector extension
Capacity: Millions of embeddings
Retention: Permanent
Access time: 10–100ms (ANN search)
Schema: (id, content, embedding, source, confidence, created_at, updated_at)
L4: Procedural Memory (Rules + SKILL.md)

Storage: PostgreSQL rules table + filesystem SKILL.md files
Capacity: Unlimited
Retention: Permanent
Access time: <5ms (rules), filesystem read (SKILL.md)
Retrieval Strategy

Always check L1 first (working memory)
For factual queries: search L3 (semantic) with similarity > 0.8
For temporal queries: search L2 (episodic) by time range
For procedural queries: check L4 (rules/skills)
Combine results with recency weighting
autoDream Consolidation Rules

Run when idle > 15 minutes
Process last N episodes where N = min(100, episodes_since_last_dream)
Extract entities and relationships → upsert to semantic memory
Identify contradictions: newer + higher confidence wins
Archive episodes older than 90 days to cold storage
Known Failure Modes

pgvector index corruption: rebuild with CREATE INDEX CONCURRENTLY
Embedding model mismatch: always store model name alongside embeddings
Duplicate facts: use cosine similarity > 0.95 for deduplication
