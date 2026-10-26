-- Phase 3: versioned scoring with explanation, AI usage ledger + budget, evidence cache.

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO app_settings (key, value) VALUES ('score_policy_version', 'v1')
ON CONFLICT (key) DO NOTHING;

ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS score_breakdown      JSONB;
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS score_v1             NUMERIC(8,2);
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS score_v2             NUMERIC(8,2);
ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS score_policy_version TEXT;

ALTER TABLE prospect_intelligence_runs ADD COLUMN IF NOT EXISTS evidence_hash TEXT;
ALTER TABLE prospect_intelligence_runs ADD COLUMN IF NOT EXISTS job_id        UUID;
ALTER TABLE prospect_intelligence_runs ADD COLUMN IF NOT EXISTS input_tokens  INTEGER;
ALTER TABLE prospect_intelligence_runs ADD COLUMN IF NOT EXISTS output_tokens INTEGER;
ALTER TABLE prospect_intelligence_runs ADD COLUMN IF NOT EXISTS estimated_cost_usd NUMERIC(10,4);
CREATE INDEX IF NOT EXISTS idx_intelligence_runs_evidence ON prospect_intelligence_runs (prospect_id, evidence_hash);

-- Every paid provider call: reserved before the call, settled with real usage after.
CREATE TABLE IF NOT EXISTS ai_usage_ledger (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    prospect_id        UUID REFERENCES prospect_matrix(id) ON DELETE SET NULL,
    job_id             UUID,
    provider           TEXT NOT NULL DEFAULT 'anthropic',
    model_name         TEXT,
    analysis_version   TEXT,
    status             TEXT NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved','settled','released','rejected')),
    reserved_usd       NUMERIC(10,4) NOT NULL DEFAULT 0,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    estimated_cost_usd NUMERIC(10,4),
    evidence_hash      TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ai_ledger_created ON ai_usage_ledger (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_ledger_prospect ON ai_usage_ledger (prospect_id, created_at DESC);

-- One row per calendar month; locked FOR UPDATE while reserving so concurrent
-- jobs cannot both squeeze under the ceiling.
CREATE TABLE IF NOT EXISTS ai_budget_periods (
    period       TEXT PRIMARY KEY,           -- 'YYYY-MM'
    reserved_usd NUMERIC(12,4) NOT NULL DEFAULT 0,
    settled_usd  NUMERIC(12,4) NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Scoring policy v2 (explainable). Returns the breakdown and stores score_v2.
--   contribution = strength_w * pillar_w * recency * (0.5 + 0.5*confidence) * 0.6^(rank-1 within pillar)
--   recency: <=180d 1.0 | <=365d 0.75 | <=730d 0.5 | older 0.25  (date = source_date, else detected_at)
--   fit     = 0.7 + 0.06 * likelihood   (likelihood 5 -> 1.0; range 0.76..1.30)
--   score_v2 = sum(contribution) * fit
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION calculate_prospect_score_v2(p_prospect_id UUID)
RETURNS JSONB AS $$
DECLARE
    likelihood   SMALLINT;
    fit          NUMERIC;
    pressure     NUMERIC;
    pillars      JSONB;
    latest_date  DATE;
    used         INTEGER;
    excluded     INTEGER;
    result       JSONB;
BEGIN
    SELECT pm.likelihood_score INTO likelihood FROM prospect_matrix pm WHERE pm.id = p_prospect_id;
    fit := 0.7 + 0.06 * COALESCE(likelihood, 5);

    WITH valid AS (
        SELECT id, pressure_type::text AS pillar, strength::text AS strength,
               COALESCE(source_date, detected_at::date) AS ev_date,
               COALESCE(confidence_score, 0.6) AS conf,
               CASE strength WHEN 'strong' THEN 3.0 WHEN 'moderate' THEN 2.0 ELSE 1.0 END AS sw,
               CASE pressure_type
                   WHEN 'production' THEN 4.0 WHEN 'license_to_operate' THEN 2.0
                   WHEN 'cost' THEN 1.5 WHEN 'people' THEN 1.5
                   WHEN 'quality' THEN 0.5 WHEN 'future_readiness' THEN 0.5 END AS pw
        FROM pressure_signals
        WHERE prospect_id = p_prospect_id
          AND (is_valid IS NULL OR is_valid = TRUE)
          AND superseded_by IS NULL
    ), weighted AS (
        SELECT *, sw * pw AS base,
               CASE WHEN ev_date >= CURRENT_DATE - 180 THEN 1.0
                    WHEN ev_date >= CURRENT_DATE - 365 THEN 0.75
                    WHEN ev_date >= CURRENT_DATE - 730 THEN 0.5
                    ELSE 0.25 END AS recency
        FROM valid
    ), ranked AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY pillar ORDER BY base * recency DESC, ev_date DESC) AS rn
        FROM weighted
    ), contrib AS (
        SELECT *, ROUND((base * recency * (0.5 + 0.5 * conf) * POWER(0.6, rn - 1))::numeric, 3) AS contribution
        FROM ranked
    ), per_pillar AS (
        SELECT pillar,
               ROUND(SUM(contribution), 2) AS contribution,
               COUNT(*) AS signals,
               MAX(ev_date) AS latest_evidence,
               MAX(recency) AS best_recency,
               SUM(CASE WHEN strength = 'strong' THEN 1 ELSE 0 END) AS strong
        FROM contrib GROUP BY pillar
    )
    SELECT COALESCE(SUM(contribution), 0),
           COALESCE(jsonb_object_agg(pillar, jsonb_build_object(
               'contribution', contribution, 'signals', signals, 'strong', strong,
               'latest_evidence', latest_evidence, 'best_recency', best_recency)), '{}'::jsonb),
           MAX(latest_evidence), COALESCE(SUM(signals), 0)
    INTO pressure, pillars, latest_date, used
    FROM per_pillar;

    SELECT COUNT(*) INTO excluded FROM pressure_signals
    WHERE prospect_id = p_prospect_id AND (is_valid = FALSE OR superseded_by IS NOT NULL);

    result := jsonb_build_object(
        'policy', 'v2',
        'score_v2', ROUND(pressure * fit, 2),
        'pressure_score', ROUND(pressure, 2),
        'fit_factor', ROUND(fit, 2),
        'likelihood', likelihood,
        'pillars', pillars,
        'signals_used', used,
        'signals_excluded', excluded,
        'latest_evidence_date', latest_date,
        'days_since_latest_evidence', CASE WHEN latest_date IS NULL THEN NULL ELSE (CURRENT_DATE - latest_date) END,
        'computed_at', NOW()
    );

    UPDATE prospect_matrix SET score_v2 = ROUND(pressure * fit, 2), score_breakdown = result
    WHERE id = p_prospect_id;
    RETURN result;
END;
$$ LANGUAGE plpgsql;

-- v1 kept as the default displayed score; now also excludes superseded rows,
-- records score_v1/score_v2 side by side and honours app_settings.score_policy_version.
CREATE OR REPLACE FUNCTION calculate_prospect_score(p_prospect_id UUID)
RETURNS NUMERIC AS $$
DECLARE
    signal_score NUMERIC;
    likelihood   SMALLINT;
    v1_score     NUMERIC;
    breakdown    JSONB;
    policy       TEXT;
    final_score  NUMERIC;
BEGIN
    SELECT COALESCE(SUM(
        CASE strength WHEN 'strong' THEN 3.0 WHEN 'moderate' THEN 2.0 WHEN 'weak' THEN 1.0 END
        *
        CASE pressure_type
            WHEN 'production'          THEN 4.0
            WHEN 'license_to_operate'  THEN 2.0
            WHEN 'cost'                THEN 1.5
            WHEN 'people'              THEN 1.5
            WHEN 'quality'             THEN 0.5
            WHEN 'future_readiness'    THEN 0.5
        END
    ), 0) INTO signal_score
    FROM pressure_signals
    WHERE prospect_id = p_prospect_id
      AND (is_valid IS NULL OR is_valid = TRUE)
      AND superseded_by IS NULL;

    SELECT pm.likelihood_score INTO likelihood FROM prospect_matrix pm WHERE pm.id = p_prospect_id;
    v1_score := ROUND(signal_score * COALESCE(likelihood, 5) / 10.0, 2);

    breakdown := calculate_prospect_score_v2(p_prospect_id);
    SELECT value INTO policy FROM app_settings WHERE key = 'score_policy_version';
    policy := COALESCE(policy, 'v1');
    final_score := CASE WHEN policy = 'v2' THEN (breakdown->>'score_v2')::numeric ELSE v1_score END;
    final_score := LEAST(final_score, 999.99);

    UPDATE prospect_matrix
    SET prospect_score = final_score,
        score_v1 = v1_score,
        score_policy_version = policy,
        score_breakdown = breakdown || jsonb_build_object('score_v1', v1_score, 'displayed_policy', policy),
        score_updated_at = NOW()
    WHERE id = p_prospect_id;

    RETURN final_score;
END;
$$ LANGUAGE plpgsql;

-- Tier now ignores superseded rows too, so tier and score read the same evidence set.
CREATE OR REPLACE FUNCTION calculate_lead_tier(p_prospect_id UUID)
RETURNS VARCHAR AS $$
DECLARE
    total_signals     INTEGER;
    pillars_strong    INTEGER;
    pillars_moderate  INTEGER;
    tier              VARCHAR(20);
BEGIN
    SELECT COUNT(*) INTO total_signals
    FROM pressure_signals
    WHERE prospect_id = p_prospect_id AND (is_valid IS NULL OR is_valid = TRUE) AND superseded_by IS NULL;

    SELECT COUNT(DISTINCT pressure_type) INTO pillars_strong
    FROM pressure_signals
    WHERE prospect_id = p_prospect_id AND strength = 'strong'
      AND (is_valid IS NULL OR is_valid = TRUE) AND superseded_by IS NULL;

    SELECT COUNT(DISTINCT pressure_type) INTO pillars_moderate
    FROM pressure_signals
    WHERE prospect_id = p_prospect_id AND strength IN ('strong', 'moderate')
      AND (is_valid IS NULL OR is_valid = TRUE) AND superseded_by IS NULL;

    IF total_signals = 0 THEN
        tier := 'not_qualified';
    ELSIF pillars_strong >= 3 THEN
        tier := 'hot';
    ELSIF pillars_strong >= 1 OR pillars_moderate >= 3 THEN
        tier := 'warm';
    ELSE
        tier := 'watch';
    END IF;

    UPDATE prospect_matrix SET lead_tier = tier WHERE id = p_prospect_id;
    RETURN tier;
END;
$$ LANGUAGE plpgsql;
