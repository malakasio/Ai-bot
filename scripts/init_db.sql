-- JARVIS v7.0 — 4-layer memory schema
--
-- Layers:
--   1. Working memory   — in-process, not persisted (no table here).
--   2. Episodic memory  — history_logs, append-only, time-ordered.
--   3. Semantic memory  — jarvis_semantic_memory, pgvector embeddings.
--   4. Procedural memory — skills + workflows, the "how-to" of JARVIS.
--
-- Plus an out-of-band table: task_queue for the KAIROS daemon.
--
-- Run with:
--   psql -d jarvis -f scripts/init_db.sql
--
-- Idempotent: every CREATE uses IF NOT EXISTS. Safe to re-run.

BEGIN;

-- ─── Extensions ───────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;          -- for gen_random_uuid()

-- TimescaleDB is optional. If it's loaded we hyper-table history_logs at
-- the bottom of this file. If not, history_logs stays a plain table — the
-- queries don't care.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
    EXECUTE 'CREATE EXTENSION IF NOT EXISTS timescaledb';
  END IF;
END$$;

-- ─── 2. Episodic memory ───────────────────────────────────────────────────
-- One row per significant event. Append-only by convention.
CREATE TABLE IF NOT EXISTS history_logs (
  id              BIGSERIAL,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor           TEXT NOT NULL,                    -- 'jarvis', 'user', 'kairos', skill name…
  tool            TEXT NOT NULL,                    -- 'bash', 'router:anthropic', 'mcp:filesystem'…
  input           TEXT,                             -- redact secrets before insert
  output          TEXT,
  exit_code       INTEGER,
  zone            TEXT,                             -- green | yellow | red
  score           SMALLINT,                         -- 0-100, NULL if unscored
  duration_ms     INTEGER,
  failure_mode    TEXT,                             -- hallucination | wrong-tool | …
  task_id         UUID,                             -- correlates with task_queue.id
  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (id, ts)
);

CREATE INDEX IF NOT EXISTS history_logs_ts_idx     ON history_logs (ts DESC);
CREATE INDEX IF NOT EXISTS history_logs_actor_idx  ON history_logs (actor, ts DESC);
CREATE INDEX IF NOT EXISTS history_logs_task_idx   ON history_logs (task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS history_logs_failure_idx ON history_logs (failure_mode, ts DESC)
  WHERE failure_mode IS NOT NULL;

-- ─── 3. Semantic memory ───────────────────────────────────────────────────
-- pgvector with 1536-dim embeddings (OpenAI text-embedding-3-small /
-- Cohere / many others). Change vector(1536) if your embedder differs;
-- match PGVECTOR_DIM in .env.
CREATE TABLE IF NOT EXISTS jarvis_semantic_memory (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_episode  BIGINT,                           -- optional FK to history_logs.id
  kind            TEXT NOT NULL,                    -- 'fact' | 'entity' | 'preference' | 'relation'
  subject         TEXT NOT NULL,                    -- canonical entity / topic
  content         TEXT NOT NULL,                    -- the fact, in natural language
  confidence      REAL NOT NULL DEFAULT 0.5         -- 0.0-1.0
                    CHECK (confidence >= 0 AND confidence <= 1),
  observation_count INTEGER NOT NULL DEFAULT 1,
  last_observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  embedding       vector(1536) NOT NULL,
  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS jarvis_semantic_memory_subject_idx
  ON jarvis_semantic_memory (subject);
CREATE INDEX IF NOT EXISTS jarvis_semantic_memory_kind_idx
  ON jarvis_semantic_memory (kind);

-- HNSW ANN index over cosine distance. Tune ef_construction / m if recall
-- or build time becomes a problem.
CREATE INDEX IF NOT EXISTS jarvis_semantic_memory_embedding_hnsw
  ON jarvis_semantic_memory
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- ─── 4. Procedural memory ─────────────────────────────────────────────────
-- "How-to". Skills are the things JARVIS knows how to do; workflows are
-- ordered compositions of skills. Both are first-class.

CREATE TABLE IF NOT EXISTS skills (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT UNIQUE NOT NULL,             -- matches skills/<name>/SKILL.md
  version         TEXT NOT NULL DEFAULT '1.0',
  description     TEXT,
  skill_md_path   TEXT,                             -- relative path to SKILL.md
  inputs_schema   JSONB NOT NULL DEFAULT '{}'::jsonb,
  outputs_schema  JSONB NOT NULL DEFAULT '{}'::jsonb,
  success_count   INTEGER NOT NULL DEFAULT 0,
  failure_count   INTEGER NOT NULL DEFAULT 0,
  last_run_at     TIMESTAMPTZ,
  flagged_for_review BOOLEAN NOT NULL DEFAULT FALSE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS skills_flagged_idx ON skills (flagged_for_review)
  WHERE flagged_for_review = TRUE;

CREATE TABLE IF NOT EXISTS workflows (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT UNIQUE NOT NULL,
  description     TEXT,
  -- ordered list of {skill_name, args, on_failure} steps
  steps           JSONB NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Lessons learned: short notes appended by the self-improvement loop.
-- Mirrors what gets written into skills/<name>/SKILL.md "Lessons learned".
CREATE TABLE IF NOT EXISTS skill_lessons (
  id              BIGSERIAL PRIMARY KEY,
  skill_id        UUID NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  score           SMALLINT,
  failure_mode    TEXT,
  lesson          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS skill_lessons_skill_ts_idx
  ON skill_lessons (skill_id, ts DESC);

-- ─── Task queue (KAIROS daemon) ───────────────────────────────────────────
-- Drained every KAIROS_INTERVAL seconds. SELECT … FOR UPDATE SKIP LOCKED
-- is the safe checkout pattern.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'task_status') THEN
    CREATE TYPE task_status AS ENUM (
      'queued', 'running', 'done', 'failed', 'cancelled'
    );
  END IF;
END$$;

CREATE TABLE IF NOT EXISTS task_queue (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  scheduled_for   TIMESTAMPTZ NOT NULL DEFAULT now(),
  priority        SMALLINT NOT NULL DEFAULT 100,    -- lower = higher priority
  status          task_status NOT NULL DEFAULT 'queued',
  kind            TEXT NOT NULL,                    -- 'skill' | 'workflow' | 'oneshot'
  target          TEXT NOT NULL,                    -- skill name, workflow name, or shell line
  payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
  attempts        SMALLINT NOT NULL DEFAULT 0,
  max_attempts    SMALLINT NOT NULL DEFAULT 3,
  locked_by       TEXT,                             -- worker id
  locked_at       TIMESTAMPTZ,
  started_at      TIMESTAMPTZ,
  finished_at     TIMESTAMPTZ,
  result          JSONB,
  error           TEXT,
  notify          BOOLEAN NOT NULL DEFAULT FALSE    -- KAIROS push notification on completion
);

-- The hot index for the queue drain: queued tasks ready to run, ordered.
CREATE INDEX IF NOT EXISTS task_queue_drain_idx
  ON task_queue (priority, scheduled_for)
  WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS task_queue_status_idx ON task_queue (status);

-- ─── updated_at triggers ──────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS skills_set_updated_at ON skills;
CREATE TRIGGER skills_set_updated_at
  BEFORE UPDATE ON skills
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS workflows_set_updated_at ON workflows;
CREATE TRIGGER workflows_set_updated_at
  BEFORE UPDATE ON workflows
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS jarvis_semantic_memory_set_updated_at ON jarvis_semantic_memory;
CREATE TRIGGER jarvis_semantic_memory_set_updated_at
  BEFORE UPDATE ON jarvis_semantic_memory
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ─── Optional: TimescaleDB hypertable on history_logs ─────────────────────
-- Only runs if the extension is installed. If not, history_logs stays a
-- regular partitioned-by-nothing table — fine for low-to-medium volume.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    PERFORM create_hypertable(
      'history_logs',
      'ts',
      chunk_time_interval => INTERVAL '7 days',
      if_not_exists => TRUE,
      migrate_data => TRUE
    );
  END IF;
END$$;

COMMIT;
