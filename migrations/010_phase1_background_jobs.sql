-- Phase 1: durable background jobs (replaces in-memory progress dicts).
-- One row per unit of background work. Progress/result are JSONB so each job
-- type can keep the shape the dashboard already understands.

CREATE TABLE IF NOT EXISTS background_jobs (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_type          TEXT NOT NULL,
    params            JSONB NOT NULL DEFAULT '{}',
    idempotency_key   TEXT,
    status            TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    priority          SMALLINT NOT NULL DEFAULT 5,
    attempts          SMALLINT NOT NULL DEFAULT 0,
    max_attempts      SMALLINT NOT NULL DEFAULT 2,
    progress          JSONB NOT NULL DEFAULT '{}',
    result            JSONB,
    error             TEXT,
    cancel_requested  BOOLEAN NOT NULL DEFAULT FALSE,
    worker_id         TEXT,
    heartbeat_at      TIMESTAMPTZ,
    lease_expires_at  TIMESTAMPTZ,
    triggered_by      TEXT NOT NULL DEFAULT 'api',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Only one queued/running job per idempotency key (e.g. one ASX refresh at a time).
CREATE UNIQUE INDEX IF NOT EXISTS uq_background_jobs_active_key
    ON background_jobs (idempotency_key)
    WHERE idempotency_key IS NOT NULL AND status IN ('queued','running');

CREATE INDEX IF NOT EXISTS idx_background_jobs_claim
    ON background_jobs (status, priority, created_at)
    WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS idx_background_jobs_type_created
    ON background_jobs (job_type, created_at DESC);

COMMENT ON TABLE background_jobs IS 'Durable queue for refresh, enrichment, contact and AI analysis work. Survives restarts; claimed with SKIP LOCKED.';

-- Operational events (job failures, alert conditions, restores, etc.)
CREATE TABLE IF NOT EXISTS ops_events (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_type  TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warning','error')),
    message     TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ops_events_created ON ops_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_events_type_created ON ops_events (event_type, created_at DESC);

-- Snapshot provenance: keep the evidence date (as_of in the file) separate from
-- the import timestamp, and record who last wrote the row so startup seeding
-- never overwrites analyst edits.
ALTER TABLE pitchbook_snapshots ADD COLUMN IF NOT EXISTS source_as_of DATE;
ALTER TABLE pitchbook_snapshots ADD COLUMN IF NOT EXISTS imported_at  TIMESTAMPTZ;
ALTER TABLE pitchbook_snapshots ADD COLUMN IF NOT EXISTS edited_at    TIMESTAMPTZ;
ALTER TABLE pitchbook_snapshots ADD COLUMN IF NOT EXISTS source_file  TEXT;
