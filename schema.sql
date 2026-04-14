-- ============================================================================
-- DELTA PROSPECT SYSTEM v2.0 — Database Schema
-- PostgreSQL 15+
-- ============================================================================
-- Supports the full New Delta Prospect System pipeline:
--   Phase 1: ASX ingestion → sector filter → enrichment → scoring → qualification
--   Phase 2: (future) outreach tracking, message drafts, follow-up cadences
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ============================================================================
-- ENUMS
-- ============================================================================

DO $$ BEGIN
    CREATE TYPE prospect_status AS ENUM (
        'unscreened',
        'qualified',
        'enriched',
        'ready_for_outreach',
        'suggested_dq',
        'disqualified',
        'archived'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE enrichment_source AS ENUM (
        'asx_announcement',
        'annual_report',
        'quarterly_report',
        'investor_presentation',
        'media_article',
        'manual_entry'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE pressure_type AS ENUM (
        'operational',
        'cost',
        'safety',
        'governance',
        'environmental',
        'market',
        'workforce'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE signal_strength AS ENUM (
        'weak',
        'moderate',
        'strong'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================================
-- CORE TABLES
-- ============================================================================

-- Master list: every ASX-listed entity, refreshed weekly from the CSV feed
CREATE TABLE IF NOT EXISTS asx_listings (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ticker               VARCHAR(10) NOT NULL,
    company_name         TEXT NOT NULL,
    gics_industry_group  TEXT,
    gics_sector          TEXT,
    is_target_sector     BOOLEAN DEFAULT FALSE,
    listing_date         DATE,
    market_cap_aud       BIGINT,          -- cents (avoids float drift)
    last_price_aud       INTEGER,         -- cents
    website              TEXT,
    principal_activities TEXT,
    first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_refreshed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active            BOOLEAN DEFAULT TRUE,
    delisted_at          TIMESTAMPTZ,
    CONSTRAINT uq_ticker UNIQUE (ticker)
);

CREATE INDEX IF NOT EXISTS idx_listings_target   ON asx_listings (is_target_sector) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_listings_sector   ON asx_listings (gics_sector)      WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_listings_ticker   ON asx_listings (ticker);
CREATE INDEX IF NOT EXISTS idx_listings_name_trg ON asx_listings USING gin (company_name gin_trgm_ops);

-- ============================================================================
-- PROSPECT MATRIX — the filtered, scored, working set
-- ============================================================================

CREATE TABLE IF NOT EXISTS prospect_matrix (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    listing_id               UUID NOT NULL REFERENCES asx_listings(id) ON DELETE CASCADE,
    status                   prospect_status NOT NULL DEFAULT 'unscreened',
    status_changed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status_changed_by        TEXT,
    dq_reason                TEXT,

    -- Strategic profile (populated by enrichment agent)
    strategic_direction      TEXT,
    primary_tailwind         TEXT,
    primary_headwind         TEXT,
    likelihood_score         SMALLINT CHECK (likelihood_score BETWEEN 1 AND 10),

    -- Composite score
    prospect_score           NUMERIC(5,2),
    score_updated_at         TIMESTAMPTZ,

    -- Buyer identification
    primary_buyer_name       TEXT,
    primary_buyer_role       TEXT,
    primary_buyer_linkedin   TEXT,
    secondary_buyer_name     TEXT,
    secondary_buyer_role     TEXT,
    secondary_buyer_linkedin TEXT,

    -- Network intelligence
    network_path             TEXT,
    warm_intro_contact       TEXT,

    analyst_notes            TEXT,
    is_watchlisted           BOOLEAN DEFAULT FALSE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_listing_prospect UNIQUE (listing_id)
);

CREATE INDEX IF NOT EXISTS idx_prospect_status ON prospect_matrix (status);
CREATE INDEX IF NOT EXISTS idx_prospect_score  ON prospect_matrix (prospect_score DESC NULLS LAST);

-- ============================================================================
-- PRESSURE SIGNALS — the core intelligence layer
-- ============================================================================

CREATE TABLE IF NOT EXISTS pressure_signals (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    prospect_id      UUID NOT NULL REFERENCES prospect_matrix(id) ON DELETE CASCADE,
    pressure_type    pressure_type NOT NULL,
    strength         signal_strength NOT NULL DEFAULT 'moderate',
    summary          TEXT NOT NULL,
    source_type      enrichment_source NOT NULL,
    source_url       TEXT,
    source_title     TEXT,
    source_date      DATE,
    extracted_quote  TEXT,
    confidence_score NUMERIC(3,2) CHECK (confidence_score BETWEEN 0 AND 1),
    model_version    TEXT,
    detected_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    validated_by     TEXT,
    validated_at     TIMESTAMPTZ,
    is_valid         BOOLEAN,
    CONSTRAINT uq_signal_source UNIQUE (prospect_id, pressure_type, source_url)
);

CREATE INDEX IF NOT EXISTS idx_signals_prospect ON pressure_signals (prospect_id);
CREATE INDEX IF NOT EXISTS idx_signals_type     ON pressure_signals (pressure_type);
CREATE INDEX IF NOT EXISTS idx_signals_strength ON pressure_signals (strength);

-- ============================================================================
-- ENRICHMENT LOG — audit trail
-- ============================================================================

CREATE TABLE IF NOT EXISTS enrichment_log (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    listing_id          UUID NOT NULL REFERENCES asx_listings(id) ON DELETE CASCADE,
    action              TEXT NOT NULL,
    source_type         enrichment_source,
    source_url          TEXT,
    success             BOOLEAN NOT NULL,
    error_message       TEXT,
    documents_processed INTEGER DEFAULT 0,
    signals_found       INTEGER DEFAULT 0,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    duration_ms         INTEGER,
    tokens_used         INTEGER,
    triggered_by        TEXT NOT NULL DEFAULT 'system',
    agent_version       TEXT
);

CREATE INDEX IF NOT EXISTS idx_enrich_listing ON enrichment_log (listing_id);
CREATE INDEX IF NOT EXISTS idx_enrich_started ON enrichment_log (started_at DESC);

-- ============================================================================
-- GICS SECTOR MAP — reference table for sector filtering
-- ============================================================================

CREATE TABLE IF NOT EXISTS gics_sector_map (
    industry_group TEXT PRIMARY KEY,
    gics_sector    TEXT NOT NULL,
    is_target      BOOLEAN NOT NULL DEFAULT FALSE,
    notes          TEXT
);

INSERT INTO gics_sector_map (industry_group, gics_sector, is_target, notes) VALUES
    ('Energy',                                        'Energy',                 TRUE,  'Oil, gas, consumable fuels, energy equipment & services'),
    ('Materials',                                     'Materials',              TRUE,  'Metals & mining, chemicals, construction materials'),
    ('Capital Goods',                                 'Industrials',            TRUE,  'Aerospace, building products, construction & engineering, machinery'),
    ('Utilities',                                     'Utilities',              TRUE,  'Electric, gas, water utilities; renewable energy'),
    ('Commercial & Professional Services',            'Industrials',            FALSE, NULL),
    ('Transportation',                                'Industrials',            FALSE, NULL),
    ('Automobiles & Components',                      'Consumer Discretionary', FALSE, NULL),
    ('Consumer Discretionary Distribution & Retail',  'Consumer Discretionary', FALSE, NULL),
    ('Consumer Durables & Apparel',                   'Consumer Discretionary', FALSE, NULL),
    ('Consumer Services',                             'Consumer Discretionary', FALSE, NULL),
    ('Media & Entertainment',                         'Communication Services', FALSE, NULL),
    ('Telecommunication Services',                    'Communication Services', FALSE, NULL),
    ('Consumer Staples Distribution & Retail',        'Consumer Staples',       FALSE, NULL),
    ('Food, Beverage & Tobacco',                      'Consumer Staples',       FALSE, NULL),
    ('Household & Personal Products',                 'Consumer Staples',       FALSE, NULL),
    ('Banks',                                         'Financials',             FALSE, NULL),
    ('Diversified Financials',                        'Financials',             FALSE, NULL),
    ('Financial Services',                            'Financials',             FALSE, NULL),
    ('Insurance',                                     'Financials',             FALSE, NULL),
    ('Health Care Equipment & Services',              'Health Care',            FALSE, NULL),
    ('Pharmaceuticals, Biotechnology & Life Sciences','Health Care',            FALSE, NULL),
    ('Software & Services',                           'Information Technology', FALSE, NULL),
    ('Technology Hardware & Equipment',               'Information Technology', FALSE, NULL),
    ('Semiconductors & Semiconductor Equipment',      'Information Technology', FALSE, NULL),
    ('Equity Real Estate Investment Trusts (REITs)',  'Real Estate',            FALSE, NULL),
    ('Real Estate Management & Development',          'Real Estate',            FALSE, NULL),
    ('Not Applic',                                    'Other',                  FALSE, 'ETFs, LICs, trusts')
ON CONFLICT (industry_group) DO UPDATE SET
    gics_sector = EXCLUDED.gics_sector,
    is_target   = EXCLUDED.is_target,
    notes       = EXCLUDED.notes;

-- ============================================================================
-- REFRESH RUNS — tracks weekly + on-demand refresh cycles
-- ============================================================================

CREATE TABLE IF NOT EXISTS refresh_runs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_type            TEXT NOT NULL CHECK (run_type IN ('weekly','manual','on_demand_single')),
    total_listings      INTEGER,
    new_listings        INTEGER DEFAULT 0,
    updated_listings    INTEGER DEFAULT 0,
    delisted_count      INTEGER DEFAULT 0,
    target_sector_count INTEGER DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed')),
    error_message       TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    triggered_by        TEXT NOT NULL DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS idx_refresh_started ON refresh_runs (started_at DESC);

-- ============================================================================
-- TRIGGERS
-- ============================================================================

CREATE OR REPLACE FUNCTION update_prospect_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    IF OLD.status IS DISTINCT FROM NEW.status THEN
        NEW.status_changed_at = NOW();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$ BEGIN
    CREATE TRIGGER trg_prospect_updated
        BEFORE UPDATE ON prospect_matrix
        FOR EACH ROW EXECUTE FUNCTION update_prospect_timestamp();
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================================
-- SCORING FUNCTION
-- ============================================================================
-- score = Σ(strength_weight × type_weight) × likelihood / 10
--   strength:  strong=3, moderate=2, weak=1
--   type:      operational=1.5, cost=1.3, safety=1.2,
--              environmental=1.1, governance=1.0, workforce=1.0, market=0.8

CREATE OR REPLACE FUNCTION calculate_prospect_score(p_prospect_id UUID)
RETURNS NUMERIC AS $$
DECLARE
    signal_score NUMERIC;
    likelihood   SMALLINT;
    final_score  NUMERIC;
BEGIN
    SELECT COALESCE(SUM(
        CASE strength WHEN 'strong' THEN 3.0 WHEN 'moderate' THEN 2.0 WHEN 'weak' THEN 1.0 END
        *
        CASE pressure_type
            WHEN 'operational'    THEN 1.5
            WHEN 'cost'           THEN 1.3
            WHEN 'safety'         THEN 1.2
            WHEN 'environmental'  THEN 1.1
            WHEN 'governance'     THEN 1.0
            WHEN 'workforce'      THEN 1.0
            WHEN 'market'         THEN 0.8
        END
    ), 0) INTO signal_score
    FROM pressure_signals
    WHERE prospect_id = p_prospect_id
      AND (is_valid IS NULL OR is_valid = TRUE);

    SELECT pm.likelihood_score INTO likelihood
    FROM prospect_matrix pm WHERE pm.id = p_prospect_id;

    final_score := signal_score * COALESCE(likelihood, 5) / 10.0;

    UPDATE prospect_matrix
    SET prospect_score = final_score, score_updated_at = NOW()
    WHERE id = p_prospect_id;

    RETURN final_score;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- VIEWS
-- ============================================================================

CREATE OR REPLACE VIEW v_prospect_dashboard AS
SELECT
    pm.id AS prospect_id, l.ticker, l.company_name, l.gics_sector,
    l.gics_industry_group, l.market_cap_aud, l.last_price_aud, l.website,
    pm.status, pm.prospect_score, pm.strategic_direction,
    pm.primary_tailwind, pm.primary_headwind, pm.likelihood_score,
    pm.primary_buyer_name, pm.primary_buyer_role, pm.network_path,
    pm.analyst_notes, pm.updated_at,
    COUNT(ps.id) AS total_signals,
    COUNT(ps.id) FILTER (WHERE ps.strength = 'strong') AS strong_signals,
    COUNT(ps.id) FILTER (WHERE ps.is_valid = TRUE)     AS validated_signals,
    MAX(ps.source_date) AS latest_signal_date,
    MODE() WITHIN GROUP (ORDER BY ps.pressure_type) AS dominant_pressure_type
FROM prospect_matrix pm
JOIN asx_listings l ON l.id = pm.listing_id
LEFT JOIN pressure_signals ps ON ps.prospect_id = pm.id
WHERE l.is_active = TRUE
GROUP BY pm.id, l.id;

CREATE OR REPLACE VIEW v_sector_summary AS
SELECT
    l.gics_sector, l.gics_industry_group,
    COUNT(*) AS total_companies,
    COUNT(pm.id) AS in_matrix,
    COUNT(pm.id) FILTER (WHERE pm.status = 'qualified')          AS qualified,
    COUNT(pm.id) FILTER (WHERE pm.status = 'enriched')           AS enriched,
    COUNT(pm.id) FILTER (WHERE pm.status = 'ready_for_outreach') AS ready_for_outreach,
    AVG(pm.prospect_score) AS avg_score
FROM asx_listings l
LEFT JOIN prospect_matrix pm ON pm.listing_id = l.id
WHERE l.is_target_sector = TRUE AND l.is_active = TRUE
GROUP BY l.gics_sector, l.gics_industry_group
ORDER BY total_companies DESC;

-- ============================================================================
-- TABLE COMMENTS
-- ============================================================================

COMMENT ON TABLE asx_listings      IS 'Master list of all ASX-listed entities. Source: asx.com.au/asx/research/ASXListedCompanies.csv';
COMMENT ON TABLE prospect_matrix   IS 'Filtered/scored subset in target sectors. Core working table.';
COMMENT ON TABLE pressure_signals  IS 'Pressure signals extracted from public filings by the enrichment agent.';
COMMENT ON TABLE enrichment_log    IS 'Audit trail for all automated data pulls and analysis runs.';
COMMENT ON TABLE gics_sector_map   IS 'Reference mapping: ASX GICS industry groups → sectors & target classification.';
COMMENT ON TABLE refresh_runs      IS 'Tracks weekly and on-demand data refresh cycles.';
