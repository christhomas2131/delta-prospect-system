-- Phase 4: analyst workflow — ownership, next action, opportunity stage,
-- activity timeline, optimistic concurrency.

ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS owner             TEXT;
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS next_action       TEXT;
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS next_action_due   DATE;
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS last_contacted_at DATE;
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS opportunity_stage TEXT NOT NULL DEFAULT 'none';
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS stage_changed_at  TIMESTAMPTZ;
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS outcome_reason    TEXT;
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS row_version       INTEGER NOT NULL DEFAULT 1;

DO $$ BEGIN
    ALTER TABLE prospect_matrix ADD CONSTRAINT chk_opportunity_stage CHECK (
        opportunity_stage IN ('none','researching','outreach_planned','contacted','meeting','proposal','won','lost')
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS idx_prospect_stage ON prospect_matrix (opportunity_stage) WHERE opportunity_stage <> 'none';
CREATE INDEX IF NOT EXISTS idx_prospect_next_action_due ON prospect_matrix (next_action_due) WHERE next_action_due IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_prospect_owner ON prospect_matrix (owner) WHERE owner IS NOT NULL;

-- Timeline of everything that happened to a prospect (human and machine).
CREATE TABLE IF NOT EXISTS prospect_activity (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    prospect_id   UUID NOT NULL REFERENCES prospect_matrix(id) ON DELETE CASCADE,
    activity_type TEXT NOT NULL,       -- field_change | status_change | stage_change | note | signal_review |
                                       -- enrichment | deep_analysis | contact_enrichment | report_export |
                                       -- dq_reinstated | watchlist | snapshot
    actor         TEXT NOT NULL DEFAULT 'api',
    field         TEXT,
    old_value     TEXT,
    new_value     TEXT,
    note          TEXT,
    payload       JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_activity_prospect_created ON prospect_activity (prospect_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_type_created ON prospect_activity (activity_type, created_at DESC);

-- Bump row_version on every update and stamp stage changes.
CREATE OR REPLACE FUNCTION update_prospect_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    NEW.row_version = COALESCE(OLD.row_version, 0) + 1;
    IF OLD.status IS DISTINCT FROM NEW.status THEN
        NEW.status_changed_at = NOW();
    END IF;
    IF OLD.opportunity_stage IS DISTINCT FROM NEW.opportunity_stage THEN
        NEW.stage_changed_at = NOW();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Analyst decisions on signals
ALTER TABLE pressure_signals ADD COLUMN IF NOT EXISTS analyst_note TEXT;
