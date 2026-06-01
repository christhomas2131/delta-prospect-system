# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

Automated prospect intelligence platform for a consultancy (New Delta) that sells operational expertise to ASX-listed heavy industry companies. Ingests ~2400 ASX companies, filters to 4 target sectors, enriches with pressure signal analysis, scores and ranks prospects, and serves a React dashboard.

Target sectors: **Energy, Materials (metals/mining), Capital Goods (industrials), Utilities**

---

## Commands

### Backend

```bash
# Activate venv first (Windows)
venv\Scripts\activate

# Run API (dev)
uvicorn api:app --host 0.0.0.0 --port 8000 --reload

# ASX scraper
python asx_scraper.py --mode full                        # Full ingest + Phase 0 DQ gates
python asx_scraper.py --mode full --dry-run              # Preview Gate 1 + Gate 2 without writing
python asx_scraper.py --mode single --ticker BHP

# Enrichment agent (rule-based, free)
python enrichment_agent.py --mode batch                  # All unscreened/qualified prospects
python enrichment_agent.py --mode single --ticker BHP
python enrichment_agent.py --mode rescore                # Recalculate scores only
```

### Frontend

```bash
cd frontend
npm run dev        # Vite dev server → localhost:5173
npm run build      # Production build → dist/
npm run lint
```

### Tests

Integration tests require local PostgreSQL with dev credentials (`delta` / `delta_dev` / `delta_prospect`).

```bash
pytest tests/ -v                              # All tests
pytest tests/test_phase0_gate1.py -v         # Gate 1 (market cap threshold)
pytest tests/test_phase0_gate2.py -v         # Gate 2 (zero volume)
pytest tests/test_leaderboard_dq_filter.py -v # Leaderboard DQ filter (requires API on :8000)
pytest tests/test_snapshot.py -v             # Snapshot API roundtrip (mocked DB, no PG needed)
```

Set env vars for integration tests: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` (defaults match dev config).

### Database

```bash
# Start PostgreSQL (Docker)
docker run -d --name delta-pg \
  -e POSTGRES_USER=delta -e POSTGRES_PASSWORD=delta_dev \
  -e POSTGRES_DB=delta_prospect -p 5432:5432 postgres:15

# Apply schema (safe to re-run — all migrations use IF NOT EXISTS / DO $$ blocks)
psql -U delta -d delta_prospect -f schema.sql
```

---

## Architecture

```
ASX CSV + ASX JSON API (Markit Digital)
    ↓
asx_scraper.py
  ├─ Parse ~2400 listings → asx_listings table
  ├─ Create prospect_matrix rows for target sectors
  ├─ fetch_company_detail(): market_cap_aud, last_price_aud, day_volume, location
  ├─ apply_gate1_dq(): cap > 0 AND cap < AUD $5M → disqualified
  └─ apply_gate2_dq(): volume = 0 AND 0 < cap < AUD $50M → disqualified
    ↓
enrichment_agent.py
  ├─ Fetch ASX announcements (2s rate limit between companies)
  ├─ Regex pattern-match against 100+ rules across 6 pillars
  ├─ Calculate prospect_score = Σ(strength_weight × pillar_weight) × likelihood/10
  └─ Write to pressure_signals + update prospect_matrix
    ↓
api.py (FastAPI + APScheduler)
  ├─ REST API under /api/
  ├─ ThreadedConnectionPool for concurrent DB access
  ├─ Background tasks for refresh/enrich jobs
  ├─ Serves frontend static files from frontend/dist/
  └─ Seeds pitchbook_snapshots from snapshots/*.json at startup
    ↓
Frontend (React + Vite + Tailwind, React Router)
  ├─ /            → Dashboard
  ├─ /leads       → LeadMatrix (filterable prospect table)
  └─ /deep-intelligence/:id → DeepIntelligence (company detail + signals + snapshot)
```

---

## Key Patterns and Constraints

### Money is stored in cents
`market_cap_aud` and `last_price_aud` are BIGINT in cents (not dollars). Gate 1 threshold = 500,000,000 (AUD $5M). Gate 2 threshold = 5,000,000,000 (AUD $50M).

### Phase 0 auto-DQ gates
Both gates live in `asx_scraper.py` and run in sequence at the end of `run_full_refresh()`:
- **Gate 1**: Skips `market_cap_aud = 0` rows (stale/unrefreshed — not genuinely zero-cap).
- **Gate 2**: Skips `day_volume IS NULL` rows (not yet fetched). Only fires on confirmed zero.
- Neither gate overwrites already-`disqualified` or `archived` rows.
- `dq_reason` TEXT column on `prospect_matrix` stores the human-readable reason.

### Enrichment deduplication
`pressure_signals` has a unique constraint on `(prospect_id, pressure_type, source_url)`. Re-enriching a company won't duplicate signals from the same announcement.

### Status transitions
`save_results()` in `enrichment_agent.py` only advances status from `unscreened/qualified → enriched`. It will **not** flip a `disqualified` row back to `enriched`.

### Snapshot seeding
At API startup, `api.py` reads all `snapshots/*_snapshot.json` files and upserts them into `pitchbook_snapshots` by ticker. Add a new `<ticker>_snapshot.json` to the `snapshots/` dir to persist a snapshot across deploys.

### DQ'd prospects in the UI
- **Deep Intelligence leaderboard** (`/api/prospects?exclude_dq=true`): DQ'd rows excluded.
- **Lead Matrix**: DQ'd rows visible with DISQUALIFIED badge — intentional, users can filter.
- **Company detail page** (`DeepIntelligence.jsx`): Shows red hazard-stripe DQ banner above the header when `status = 'disqualified'`.

### ASX API limits
The Markit Digital JSON API is undocumented and rate-limited. The scraper uses 0.35s delay between detail requests. The ASX announcements API (used by `enrichment_agent.py`) returns max 5 announcements regardless of pagination params — this is a hard limit of the endpoint, not a bug.

### Frontend routing
`App.jsx` has redirect routes: `/prospects → /leads`, `/prospects/:id → /deep-intelligence`. `ProspectDetail.jsx` exists but is **not used** — all detail traffic goes through `DeepIntelligence.jsx`.

---

## Python Module Guide

| Module | Purpose |
|---|---|
| `api.py` | FastAPI app, all HTTP endpoints, APScheduler cron, snapshot seeding |
| `asx_scraper.py` | CSV ingest, company detail fetch, Phase 0 DQ gates |
| `enrichment_agent.py` | Rule-based signal detection, scoring, batch/single enrichment |
| `asx_browser.py` | ASX announcements fetcher (wraps the Markit API with retries) |
| `deep_analysis.py` | Claude API enrichment v1 — headline-based analysis |
| `v3_intelligence.py` | Claude API enrichment v2 — full document analysis via Firecrawl |
| `prize_calculator.py` | Size-of-prize estimation logic |
| `schema.sql` | Full DB schema + all migrations (safe to re-run) |

---

## Environment Variables

```
DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD   # PostgreSQL connection
ANTHROPIC_API_KEY   # Required only for deep analysis (not rule-based enrichment)
FIRECRAWL_API_KEY   # Required only for v3 full-document intelligence
DATABASE_URL        # Overrides individual DB_* vars if set (Railway uses this)
CRON_SECRET         # Protects POST /api/cron/enrich-all
AUTH_USER / AUTH_PASSWORD  # Basic auth (optional — disables if not set)
```

---

## Deployment (Railway)

Railway pulls from `master`. The Dockerfile builds the frontend and copies everything to the image. `schema.sql` is in the image but **not executed automatically** — schema migrations must be run manually against the Railway PostgreSQL via `psql $DATABASE_URL -f schema.sql` or the Railway dashboard query console.

See `PHASE0_ROLLBACK.md` for rollback procedures and DB backup file locations.
