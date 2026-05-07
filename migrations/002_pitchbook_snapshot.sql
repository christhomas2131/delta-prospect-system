CREATE TABLE pitchbook_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prospect_id UUID UNIQUE REFERENCES prospect_matrix(id) ON DELETE CASCADE,
    snapshot_data JSONB NOT NULL DEFAULT '{}',
    snapshot_markdown TEXT,
    enriched_at TIMESTAMP DEFAULT NOW(),
    enriched_by TEXT DEFAULT 'manual'
);
CREATE INDEX idx_pitchbook_snapshots_prospect ON pitchbook_snapshots(prospect_id);
