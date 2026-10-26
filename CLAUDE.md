# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

Automated prospect intelligence platform for a consultancy (New Delta) that sells operational expertise to ASX-listed heavy industry companies. Ingests ~1800+ companies from the ASX company directory, filters to 4 target sectors, enriches with pressure signal analysis, scores and ranks prospects, and serves a React dashboard.

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

# Schema migrations (schema.sql = baseline, then migrations/NNN_*.sql once each)
python migrations.py --status
python migrations.py                                     # also runs at API startup when AUTO_MIGRATE=true (default)

# Standalone job worker (only if RUN_WORKER=false on the API)
python worker.py

# Operations scripts
python scripts/backup_db.py [--verify]                   # pg_dump (+ restore rehearsal into a scratch DB)
python scripts/dedupe_ai_signals.py [--apply|--undo]     # legacy AI duplicate report / supersede
python scripts/compare_score_policies.py                 # v1 vs v2 ranking movers
python scripts/evaluate_ranking.py --export evaluation/evaluation_set.csv   # label set for precision@K
python scripts/explain_hot_queries.py                    # EXPLAIN ANALYZE on the hot read paths
```

### Frontend

```bash
cd frontend
npm run dev        # Vite dev server → localhost:5173
npm run build      # Production build → dist/
npm run lint       # CI runs with --max-warnings=0
npm test -- --run  # vitest (api client, polling backoff, redirect, ScoreBreakdown)
```

### Tests

Integration tests require local PostgreSQL with dev credentials (`delta` / `delta_dev` / `delta_prospect`).

```bash
pytest tests -q -p no:cacheprovider --basetemp=tmp/pytest --ignore=tests/test_leaderboard_dq_filter.py
pytest tests/test_jobs_queue.py -v            # durable queue: idempotency, SKIP LOCKED, lease reclaim, resume
pytest tests/test_migrations_runner.py -v     # fresh install + upgrade in a scratch schema/database
pytest tests/test_scoring_v2.py -v            # recency decay, damping, tier/score alignment
pytest tests/test_ai_contract_and_budget.py -v # AI schema validation, budget concurrency, evidence cache
pytest tests/test_workflow_and_ops.py -v      # PATCH 409, activity, review queue, DQ reinstate, ops/metrics
pytest tests/test_leaderboard_dq_filter.py -v # requires API on :8000
```

`tests/conftest.py` sets `ANTHROPIC_API_KEY=""`, `RUN_WORKER=false`, `SCHEDULER_ENABLED=false` so tests never spend money or start background work. DB fixtures create `ZZxx` tickers and delete them. On this Windows machine use `--basetemp=tmp/pytest` (the default pytest temp dir is not writable).

Set env vars for integration tests: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` (defaults match dev config).

### Database

```bash
# Start PostgreSQL (Docker)
docker run -d --name delta-pg \
  -e POSTGRES_USER=delta -e POSTGRES_PASSWORD=delta_dev \
  -e POSTGRES_DB=delta_prospect -p 5432:5432 postgres:15

# Apply schema: schema.sql is the baseline (run once per DB by the runner), new changes go in migrations/NNN_*.sql
python migrations.py
```

---

## Architecture

```
ASX CSV + ASX JSON API (Markit Digital)
    ↓
asx_scraper.py
  ├─ Parse ~1800+ companies → asx_listings table
  ├─ Create prospect_matrix rows for target sectors
  ├─ fetch_company_detail(): market_cap_aud, last_price_aud, day_volume, location
  ├─ apply_gate1_dq(): cap > 0 AND cap < AUD $5M → disqualified
  └─ apply_gate2_dq(): volume = 0 AND 0 < cap < AUD $50M → disqualified
    ↓
enrichment_agent.py
  ├─ Fetch ASX announcements (2s rate limit between companies)
  ├─ Regex pattern-match against 100+ rules across 6 pillars
  ├─ Write to pressure_signals (evidence_key, verification_status, revision_count on re-sight)
  └─ Score AND tier recalculated in SQL from the persisted valid signal set
    ↓
jobs.py + worker.py (PostgreSQL job queue — background_jobs table)
  ├─ enqueue() with idempotency keys; claim_next() with FOR UPDATE SKIP LOCKED
  ├─ heartbeat/lease (90s) → reclaim_expired() re-queues jobs from dead workers
  ├─ JobProgress persists progress JSON; handlers resume via progress['done']
  └─ handlers registered in api.py: asx_refresh, enrich_batch, enrich_single, contact_batch, deep_analysis
    ↓
api.py (FastAPI + APScheduler + in-process worker thread)
  ├─ init_runtime(): pool → migrations.run_all() → seed_snapshots() (insert-only) → API key
  ├─ REST API under /api/ ; request-id middleware ; auth middleware (warn-only when unset)
  ├─ Scheduler enqueues the daily refresh + runs hourly alert checks (alerts.py → ops_events + webhook)
  ├─ Deep analysis: ai_budget (reserve → call → settle), evidence-hash cache, ai_schema validation
  ├─ Serves frontend static files from frontend/dist/
    ↓
Frontend (React + Vite + Tailwind, React Router)
  ├─ /            → Dashboard (job-backed progress via usePoll)
  ├─ /leads       → LeadMatrix (URL-synced filters, saved views, abortable loads)
  ├─ /review      → ReviewQueue (overdue / new evidence / stale research / DQ review)
  ├─ /deep-intelligence/:id → DeepIntelligence (+ ScoreBreakdown, WorkflowPanel, signal accept/reject)
  └─ /ops         → Operations (freshness, jobs, events, spend, funnel, score policy)
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

### Enrichment deduplication and evidence identity
`pressure_signals` has a unique constraint on `(prospect_id, pressure_type, source_url)`. Re-enriching a company won't duplicate signals from the same announcement; a re-sighting bumps `revision_count`/`last_seen_at`. AI findings use **stable keys** (`evidence.py`): `evidence_key = sha1(ticker|pillar|normalised claim)` and `source_url` = the cited document URL, else `claude-deep://T/pillar/<key16>` — never time-based. `verification_status` tracks rule_matched / ai_generated / ai_confirmed / ai_disputed / analyst_confirmed / analyst_rejected / unverified / superseded. Only `is_valid IS NOT FALSE AND superseded_by IS NULL` rows count toward score, tier and prize.

### Background jobs
All long work is a row in `background_jobs` (see `jobs.py`). One active job per idempotency key (`asx_refresh`, `enrich_batch`, `contact_batch`, `deep_analysis:<id>`, `enrich_single:<T>`). Status endpoints (`/api/refresh/status`, `/api/enrich/status`, `/api/contacts/status`, `/api/prospects/{id}/deep-analysis/status`) read the latest job, so progress survives restarts. Cancel with `POST /api/jobs/{id}/cancel`. The worker runs in-process by default (`RUN_WORKER=true`).

### Scoring policies
`calculate_prospect_score()` computes **v1** (legacy) and **v2** (recency decay, per-pillar geometric damping, confidence, fit factor) every time and stores `score_v1`, `score_v2`, `score_breakdown`. `app_settings.score_policy_version` (default `v1`) decides which one is `prospect_score`. Switch via `PUT /api/settings/score-policy` after reviewing `scripts/compare_score_policies.py`.

### Paid analysis controls
`ai_budget.py`: reservation against `ai_budget_periods` (row-locked) before every Claude call, settled with real token counts into `ai_usage_ledger`. `AI_MONTHLY_BUDGET_USD=0` means no ceiling. Identical evidence (`evidence_hash`) within `AI_CACHE_MAX_AGE_DAYS` reuses the stored run instead of calling the provider. `ai_schema.py` validates the model's JSON; claims that don't cite a supplied document are stored as `unverified` and excluded from the score.

### Analyst workflow
`prospect_matrix` carries `owner`, `next_action`, `next_action_due`, `opportunity_stage`, `outcome_reason`, `row_version`. `PATCH /api/prospects/{id}` accepts `expected_version` (409 on mismatch) and writes every change to `prospect_activity`. `PATCH /api/signals/{id}` accepts/rejects a signal and rescores. `GET /api/review-queue` is the daily list.

### Status transitions
`save_results()` in `enrichment_agent.py` only advances status from `unscreened/qualified → enriched`. It will **not** flip a `disqualified` row back to `enriched`.

### Snapshot seeding
At API startup `seed_snapshots()` reads `snapshots/*_snapshot.json` and **inserts only for companies without a snapshot** (`SNAPSHOT_SEED_MODE=insert_missing`). Rows written through the API are `enriched_by='manual'` and are never overwritten, even in `overwrite` mode. The file's `as_of` is stored as `source_as_of` (evidence date) separately from `imported_at`.

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
| `api.py` | FastAPI app, all HTTP endpoints, job handlers, scheduler, snapshot seeding |
| `db.py` | Shared DB config (`DATABASE_URL` or `DB_*`) |
| `migrations.py` | Versioned migration runner (`schema_migrations` table) |
| `jobs.py` / `worker.py` | PostgreSQL job queue and worker loop |
| `evidence.py` | Stable evidence keys for AI findings; legacy duplicate grouping |
| `ai_schema.py` | Pydantic contract for Claude output; untrusted-content clause |
| `ai_budget.py` | Spend ledger, monthly budget, evidence-hash cache |
| `safe_http.py` | SSRF-safe fetch: public-IP check, redirect validation, size/type caps, Retry-After |
| `activity.py` | Prospect activity timeline |
| `alerts.py` | ops_events + webhook; hourly alert conditions |
| `observability.py` | Logging with request ids, JSON option, secret scrubbing |
| `asx_scraper.py` | CSV ingest, company detail fetch, Phase 0 DQ gates (now with reason codes) |
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
AUTH_USER / AUTH_PASSWORD  # Basic auth (optional — production without it logs a warning + Ops banner)
RUN_WORKER / SCHEDULER_ENABLED / AUTO_MIGRATE / SNAPSHOT_SEED_MODE   # process roles (see .env.example)
AI_MONTHLY_BUDGET_USD / AI_CACHE_MAX_AGE_DAYS                        # paid-analysis controls
ALERT_WEBHOOK_URL / LOG_FORMAT                                        # operations
```

---

## Deployment (Railway)

Railway pulls from `master`. The multi-stage Dockerfile builds the frontend in a Node stage and ships a Python-only runtime image. Migrations run **automatically at startup** (`AUTO_MIGRATE=true`); `/api/health` reports `migrations_pending`. Take a backup before deploying a new migration file: `DATABASE_URL=... python scripts/backup_db.py`.

See `OPERATIONS.md` for the runbook (daily checks, alerts, backup/restore, monthly routine) and `PHASE0_ROLLBACK.md` for the historical Phase 0 rollback notes.
