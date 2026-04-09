# CLAUDE.md — Delta Prospect System v2.0

## What This Project Is

An automated prospect intelligence platform for a consultancy selling operational expertise to ASX-listed heavy industry, energy, and mining companies. The system:

1. Ingests ALL companies listed on the Australian Securities Exchange (~2400)
2. Filters to target sectors: Energy, Materials (metals/mining), Capital Goods (heavy industrials), Utilities
3. Enriches each company with pressure signal analysis using a rule-based keyword engine (no API costs)
4. Scores and ranks prospects in a searchable dashboard (the "Prospect Matrix")
5. Serves everything through a FastAPI REST API and React frontend

## Project Files

| File | What It Does |
|---|---|
| `schema.sql` | PostgreSQL schema: 6 tables, 4 enums, triggers, scoring function, views, GICS seed data |
| `asx_scraper.py` | Fetches ASX CSV feed (~2400 listings), maps GICS sectors, filters targets, upserts to DB |
| `enrichment_agent.py` | Rule-based: fetches ASX announcements, pattern-matches 100+ keyword rules across 7 pressure categories, generates profiles, calculates scores. FREE, no API key needed. |
| `api.py` | FastAPI REST backend: filtering, sorting, pagination, fuzzy search, sector stats, triggers |
| `requirements.txt` | Python dependencies |
| `.env.example` | Environment config template |

## Architecture

```
ASX CSV (asx.com.au/asx/research/ASXListedCompanies.csv)
    |
    v
asx_scraper.py --> PostgreSQL (asx_listings, ~2400 rows)
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

Pressure types: operational, cost, safety, governance, environmental, market, workforce

Examples of what it catches:
- "Production downgrade" → operational/strong
- "CEO resignation" → governance/strong
- "Capital raising" → cost/strong
- "Quarterly activities report" → operational/moderate
- "Sustainability report" → environmental/weak

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
prospect_score = signal_score * (likelihood_score / 10)
signal_score = SUM(strength_weight * type_weight)
  strength: strong=3, moderate=2, weak=1
  type: operational=1.5, cost=1.3, safety=1.2, environmental=1.1, governance=1.0, workforce=1.0, market=0.8
```

Implemented as PostgreSQL function `calculate_prospect_score(UUID)`.

## API Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | /api/prospects | List with filters, sort, pagination |
| GET | /api/prospects/{id} | Detail with signals |
| PATCH | /api/prospects/{id} | Update status/notes |
| GET | /api/sectors | Sector breakdown |
| GET | /api/stats | Dashboard stats |
| POST | /api/refresh | Trigger ASX refresh |
| POST | /api/enrich/{ticker} | Trigger single enrichment |
| GET | /api/search | Fuzzy search |
| GET | /api/health | Health check |

## Environment Variables

```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=delta_prospect
DB_USER=delta
DB_PASSWORD=delta_dev
```

No API keys required. Everything runs locally for free.

## Important Notes for Claude Code

- ASX CSV URL: https://www.asx.com.au/asx/research/ASXListedCompanies.csv (two-row header, data on row 3)
- ASX JSON announcements API is undocumented, may have rate limits. We use 1.5s delay between calls.
- All money stored in CENTS (integer) to avoid float issues
- The operator is a novice coder. Explain each step simply. Fix errors inline.
- Use Docker for PostgreSQL: `docker run -d --name delta-pg -e POSTGRES_USER=delta -e POSTGRES_PASSWORD=delta_dev -e POSTGRES_DB=delta_prospect -p 5432:5432 postgres:15`
- The enrichment batch for ~800 companies takes ~20 minutes (rate-limited ASX API calls)
- The .env file should NEVER be committed to git
