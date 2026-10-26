-- Phase 2: stable evidence identity, provenance, DQ review data.

-- Signals: identity + provenance ---------------------------------------------
ALTER TABLE pressure_signals ADD COLUMN IF NOT EXISTS evidence_key        TEXT;
ALTER TABLE pressure_signals ADD COLUMN IF NOT EXISTS document_id         UUID REFERENCES announcement_documents(id) ON DELETE SET NULL;
ALTER TABLE pressure_signals ADD COLUMN IF NOT EXISTS analysis_run_id     UUID REFERENCES prospect_intelligence_runs(id) ON DELETE SET NULL;
ALTER TABLE pressure_signals ADD COLUMN IF NOT EXISTS verification_status TEXT NOT NULL DEFAULT 'rule_matched';
ALTER TABLE pressure_signals ADD COLUMN IF NOT EXISTS superseded_by       UUID REFERENCES pressure_signals(id) ON DELETE SET NULL;
ALTER TABLE pressure_signals ADD COLUMN IF NOT EXISTS superseded_at       TIMESTAMPTZ;
ALTER TABLE pressure_signals ADD COLUMN IF NOT EXISTS supersede_reason    TEXT;
ALTER TABLE pressure_signals ADD COLUMN IF NOT EXISTS revision_count      INTEGER NOT NULL DEFAULT 1;
ALTER TABLE pressure_signals ADD COLUMN IF NOT EXISTS last_seen_at        TIMESTAMPTZ;

COMMENT ON COLUMN pressure_signals.verification_status IS
  'rule_matched | ai_generated | ai_confirmed | ai_disputed | analyst_confirmed | analyst_rejected | unverified | superseded';
COMMENT ON COLUMN pressure_signals.evidence_key IS
  'Stable identity of the underlying finding (sha1 of ticker|pillar|normalised claim). Re-analysis updates the same row.';

CREATE INDEX IF NOT EXISTS idx_signals_evidence_key ON pressure_signals (prospect_id, evidence_key);
CREATE INDEX IF NOT EXISTS idx_signals_prospect_active ON pressure_signals (prospect_id)
    WHERE (is_valid IS NULL OR is_valid = TRUE) AND superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_signals_detected ON pressure_signals (detected_at DESC);

-- Backfill verification_status from existing data (one-off; new rows set it explicitly).
UPDATE pressure_signals SET verification_status = CASE
    WHEN source_url LIKE 'claude-deep://%'                      THEN 'ai_generated'
    WHEN is_valid = TRUE  AND validated_by IS NOT NULL          THEN 'ai_confirmed'
    WHEN is_valid = FALSE AND validated_by IS NOT NULL          THEN 'ai_disputed'
    ELSE 'rule_matched' END
WHERE verification_status = 'rule_matched';

-- DQ decisions: reason code + the observation that triggered them -------------
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS dq_reason_code        TEXT;
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS dq_applied_at         TIMESTAMPTZ;
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS dq_observed_cap_cents BIGINT;
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS dq_observed_volume    INTEGER;
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS dq_review_dismissed_at TIMESTAMPTZ;

UPDATE prospect_matrix pm
SET dq_reason_code = CASE
        WHEN dq_reason ILIKE '%Gate 1%' THEN 'gate1_market_cap'
        WHEN dq_reason ILIKE '%Gate 2%' THEN 'gate2_zero_volume'
        ELSE 'manual' END,
    dq_applied_at = COALESCE(dq_applied_at, status_changed_at),
    dq_observed_cap_cents = COALESCE(dq_observed_cap_cents, l.market_cap_aud),
    dq_observed_volume = COALESCE(dq_observed_volume, l.day_volume)
FROM asx_listings l
WHERE l.id = pm.listing_id AND pm.status = 'disqualified' AND pm.dq_reason_code IS NULL;

-- Market data freshness: when the ASX detail (cap/price/volume) was last fetched.
ALTER TABLE asx_listings ADD COLUMN IF NOT EXISTS detail_fetched_at TIMESTAMPTZ;
