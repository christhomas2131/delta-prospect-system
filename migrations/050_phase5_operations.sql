-- Phase 5: measured operation — indexes for the hot read paths and the funnel view.

-- list_prospects / review queue join on prospect_id and filter on strength/type;
-- the composite index lets the aggregate FILTER clauses use an index-only scan.
CREATE INDEX IF NOT EXISTS idx_signals_prospect_strength_type
    ON pressure_signals (prospect_id, strength, pressure_type)
    WHERE superseded_by IS NULL;

CREATE INDEX IF NOT EXISTS idx_listings_active_target_cap
    ON asx_listings (is_active, is_target_sector, market_cap_aud DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_prospect_watchlist ON prospect_matrix (is_watchlisted) WHERE is_watchlisted = TRUE;
CREATE INDEX IF NOT EXISTS idx_enrich_listing_completed ON enrichment_log (listing_id, completed_at DESC);

-- Funnel: one row per prospect with the stage history flattened from activity.
CREATE OR REPLACE VIEW v_opportunity_funnel AS
SELECT
    pm.id AS prospect_id, l.ticker, l.company_name, l.gics_sector,
    pm.owner, pm.opportunity_stage, pm.stage_changed_at, pm.outcome_reason,
    pm.score_policy_version, pm.prospect_score,
    to_char(COALESCE(pm.stage_changed_at, pm.updated_at), 'YYYY-MM') AS cohort_month,
    (SELECT MIN(a.created_at) FROM prospect_activity a
      WHERE a.prospect_id = pm.id AND a.activity_type = 'stage_change' AND a.new_value = 'contacted') AS first_contacted_at,
    (SELECT MIN(a.created_at) FROM prospect_activity a
      WHERE a.prospect_id = pm.id AND a.activity_type = 'stage_change' AND a.new_value = 'meeting') AS first_meeting_at,
    (SELECT MIN(a.created_at) FROM prospect_activity a
      WHERE a.prospect_id = pm.id AND a.activity_type = 'stage_change' AND a.new_value = 'proposal') AS first_proposal_at,
    (SELECT MIN(a.created_at) FROM prospect_activity a
      WHERE a.prospect_id = pm.id AND a.activity_type = 'stage_change' AND a.new_value IN ('won','lost')) AS closed_at
FROM prospect_matrix pm
JOIN asx_listings l ON l.id = pm.listing_id
WHERE pm.opportunity_stage <> 'none';
