# AGENTS.md — Delta Prospect System v2.0

## What This Project Is

An automated prospect intelligence platform for a consultancy selling operational expertise to ASX-listed heavy industry, energy, and mining companies. The system:

1. Ingests all companies in the ASX company directory (~1800+)
2. Filters to target sectors: Energy, Materials (metals/mining), Capital Goods (heavy industrials), Utilities
3. Enriches each company with pressure signal analysis using a rule-based keyword engine (no API costs)
4. Scores and ranks prospects in a searchable dashboard (the "Prospect Matrix")
5. Serves everything through a FastAPI REST API and React frontend

## Project Files

| File | What It Does |
|---|---|
| `schema.sql` | PostgreSQL schema: 6 tables, 4 enums, triggers, scoring function, views, GICS seed data |
| `asx_scraper.py` | Fetches the ASX company directory CSV (~1800+ companies), maps GICS sectors, filters targets, upserts to DB |
| `enrichment_agent.py` | Rule-based: fetches ASX announcements, pattern-matches 100+ keyword rules across the 6 New Delta pillars, generates profiles; score and tier are recalculated in SQL. FREE, no API key needed. |
| `api.py` | FastAPI REST backend: filtering, sorting, pagination, fuzzy search, sector stats, job triggers, workflow, ops |
| `migrations.py` + `migrations/` | Versioned schema migrations (schema.sql is the baseline) |
| `jobs.py` + `worker.py` | Durable PostgreSQL job queue; worker thread runs inside the API by default |
| `OPERATIONS.md` | Runbook: daily checks, alerts, backup/restore, monthly routine |
| `requirements.txt` | Python dependencies |
| `.env.example` | Environment config template |

## Architecture

```
ASX company directory CSV API
    |
    v
asx_scraper.py --> PostgreSQL (asx_listings, ~1800+ active rows)
    |                    |
    | (filter)           |
    v                    |
prospect_matrix          |
(target sectors,         |
 ~800-1000 companies)    |
    |                    |
    v                    |
enrichment_agent.py ---> ASX Announcements API (free)
    | (keyword patterns)       |
    | (no API key needed)      |
    v                    |
pressure_signals         |
    |                    |
    v                    |
api.py (FastAPI) ------> React Dashboard
```

## Enrichment Engine (Rule-Based, $0 Cost)

The enrichment agent scans ASX announcement titles against 100+ regex patterns organized by pressure type. Each pattern has an assigned strength (strong/moderate/weak) and a summary template.

Pillars (pressure_type enum): production, license_to_operate, cost, people, quality, future_readiness

Examples of what it catches:
- "Production downgrade" → production/strong
- "CEO resignation" → people/strong
- "Capital raising" → cost/strong
- "Quarterly activities report" → production/moderate
- "Environmental incident" → license_to_operate/strong

Strategic profiles are generated per sector with likelihood scores based on signal volume and strength.

## Database Tables

- **asx_listings**: All ASX companies. Key columns: ticker, company_name, gics_industry_group, gics_sector, is_target_sector, market_cap_aud (cents), is_active
- **prospect_matrix**: Scored working set. Key columns: listing_id (FK), status (enum), strategic_direction, primary_tailwind, primary_headwind, likelihood_score (1-10), prospect_score
- **pressure_signals**: Intelligence per company. Key columns: prospect_id (FK), pressure_type (enum), strength (enum), summary, confidence_score, source_url
- **enrichment_log**: Audit trail
- **gics_sector_map**: Industry group → sector mapping with target flags
- **refresh_runs**: Refresh cycle tracking

## Target Sectors

| Industry Group | Sector | Target |
|---|---|---|
| Energy | Energy | Yes |
| Materials | Materials | Yes |
| Capital Goods | Industrials | Yes |
| Utilities | Utilities | Yes |
| Everything else | Various | No |

## Scoring

```
v1 (default displayed): prospect_score = SUM(strength_weight * pillar_weight) * likelihood / 10
  strength: strong=3, moderate=2, weak=1
  pillar:   production=4.0, license_to_operate=2.0, cost=1.5, people=1.5, quality=0.5, future_readiness=0.5
v2 (computed alongside, explainable): SUM(strength*pillar*recency*(0.5+0.5*confidence)*0.6^(rank-1 per pillar)) * (0.7+0.06*likelihood)
  recency: <=180d 1.0 | <=365d 0.75 | <=730d 0.5 | older 0.25
Only valid, non-superseded signals count. Both scores + a JSON breakdown are stored on prospect_matrix.
```

Implemented as PostgreSQL functions `calculate_prospect_score(UUID)` / `calculate_prospect_score_v2(UUID)`; the displayed policy is `app_settings.score_policy_version`.

## API Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | /api/prospects | List with filters, sort, pagination |
| GET | /api/prospects/{id} | Detail with signals |
| PATCH | /api/prospects/{id} | Update status/notes |
| GET | /api/sectors | Sector breakdown |
| GET | /api/stats | Dashboard stats |
| POST | /api/refresh | Queue ASX refresh job (returns job_id) |
| POST | /api/enrich/{ticker} | Queue single enrichment job |
| POST | /api/enrich/batch | Queue batch enrichment (+ AI cost estimate) |
| GET/POST | /api/jobs, /api/jobs/{id}, /api/jobs/{id}/cancel | Job queue |
| GET | /api/review-queue | Daily analyst queue with reasons |
| GET/POST | /api/dq/review-queue, /api/prospects/{id}/dq/reinstate | DQ review |
| PATCH | /api/signals/{id} | Accept/reject a signal (rescores) |
| GET | /api/prospects/{id}/activity | Timeline |
| GET/PUT | /api/settings/score-policy | Displayed scoring policy |
| GET | /api/ai/estimate, /api/ai/ledger | Paid-analysis cost controls |
| GET | /api/ops/health, /api/ops/events, /api/metrics/funnel, /api/metrics/costs | Operations |
| GET | /api/search | Fuzzy search |
| GET | /api/health, /api/health/live | Readiness (DB + migrations, 503 when down) / liveness |

## Environment Variables

```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=delta_prospect
DB_USER=delta
DB_PASSWORD=delta_dev
```

No API keys required. Everything runs locally for free.

## Important Notes for Codex

- ASX CSV URL: https://asx.api.cmfyapp.com/asx-research/1.0/companies/directory/file (one-row header; requires the public ASX Markit token)
- ASX JSON announcements API is undocumented, may have rate limits. We use 1.5s delay between calls.
- All money stored in CENTS (integer) to avoid float issues
- The operator is a novice coder. Explain each step simply. Fix errors inline.
- Use Docker for PostgreSQL: `docker run -d --name delta-pg -e POSTGRES_USER=delta -e POSTGRES_PASSWORD=delta_dev -e POSTGRES_DB=delta_prospect -p 5432:5432 postgres:15`
- The enrichment batch for ~800 companies takes ~20 minutes (rate-limited ASX API calls)
- The .env file should NEVER be committed to git
- Schema changes: add `migrations/NNN_name.sql` (idempotent), never edit an applied migration; `python migrations.py` applies it, the API does so at startup
- Long-running work must be a job handler (`job_worker.register`) — never a FastAPI BackgroundTask or ad-hoc thread
- Tests: `pytest tests -q -p no:cacheprovider --basetemp=tmp/pytest --ignore=tests/test_leaderboard_dq_filter.py`; frontend `npm run lint -- --max-warnings=0 && npm test -- --run && npm run build`
