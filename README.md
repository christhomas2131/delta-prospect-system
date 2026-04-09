# Delta Prospect System v2.0

**Intelligence → Matrix → Conversation**

Automated prospect intelligence for ASX-listed heavy industry, energy, and mining companies. Ingests the full ASX exchange, filters to target sectors, enriches with Claude-powered pressure signal analysis, and presents a scored Prospect Matrix via REST API.

---

## Architecture

```
ASX CSV Feed (~2400 listings)
        |
        v
+----------------------------------+
|  asx_scraper.py                  |
|  Fetch > Parse > GICS map       |
|  Sector filter > Upsert > DQ    |
+---------------+------------------+
                |
                v
+----------------------------------+     +------------------------+
|  PostgreSQL                      | <-- |  enrichment_agent.py   |
|                                  |     |  ASX announcements     |
|  asx_listings        (master)    |     |  Claude analysis       |
|  prospect_matrix     (scored)    |     |  Signal extraction     |
|  pressure_signals    (intel)     |     |  Score calculation     |
|  enrichment_log      (audit)     |     +------------------------+
|  gics_sector_map     (ref)       |
|  refresh_runs        (tracking)  |
+---------------+------------------+
                |
                v
+----------------------------------+
|  api.py (FastAPI)                |
|  /api/prospects    (list/filter) |
|  /api/prospects/:id (detail)     |
|  /api/sectors      (breakdown)   |
|  /api/stats        (dashboard)   |
|  /api/refresh      (trigger)     |
|  /api/enrich/:tkr  (trigger)     |
|  /api/search       (fuzzy)       |
+----------------------------------+
```

## Target Sectors (GICS Mapping)

| Industry Group  | Sector      | Target | Covers                                          |
|-----------------|-------------|--------|-------------------------------------------------|
| Energy          | Energy      | Yes    | Oil, gas, energy equipment and services         |
| Materials       | Materials   | Yes    | Metals and mining, chemicals, construction      |
| Capital Goods   | Industrials | Yes    | Aerospace, construction, machinery              |
| Utilities       | Utilities   | Yes    | Electric, gas, water, renewables                |

Everything else (financials, tech, healthcare, etc.) stays in asx_listings
but is excluded from the Prospect Matrix.

## Scoring Formula

```
prospect_score = signal_score * (likelihood_score / 10)

signal_score = SUM(strength_weight * type_weight)
  strength:  strong=3, moderate=2, weak=1
  type:      operational=1.5  cost=1.3  safety=1.2
             environmental=1.1  governance=1.0  workforce=1.0  market=0.8
```

Example: 2 strong operational signals + likelihood 8 = (3*1.5 + 3*1.5) * 0.8 = 7.2

## Setup

### 1. Database

```bash
sudo -u postgres createuser delta
sudo -u postgres createdb delta_prospect -O delta
sudo -u postgres psql -c "ALTER USER delta WITH PASSWORD 'your_pw';"
psql -U delta -d delta_prospect -f schema.sql
```

### 2. Python

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit with your creds
```

### 3. Initial data load

```bash
python asx_scraper.py --mode full
```

### 4. Enrichment

```bash
# All unscreened prospects (batch)
python enrichment_agent.py --mode batch

# Single company
python enrichment_agent.py --mode single --ticker BHP

# Recalculate all scores (no new data)
python enrichment_agent.py --mode rescore
```

### 5. Start the API

```bash
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

### 6. Weekly cron (optional)

```cron
# Every Sunday 2am AEST (4pm UTC Saturday)
0 16 * * 0  cd /path/to/project && venv/bin/python asx_scraper.py --mode full --triggered-by weekly_cron
```

## File Structure

```
schema.sql            354 lines   PostgreSQL schema (6 tables, triggers, scoring fn, views)
asx_scraper.py        407 lines   ASX CSV ingestion, GICS mapping, sector filter, DB upsert
enrichment_agent.py   434 lines   Claude-powered announcement analysis and signal extraction
api.py                499 lines   FastAPI REST backend with filtering, search, triggers
requirements.txt       23 lines   Python dependencies
.env.example                      Environment config template
README.md                         This file
```

## Phase 2 Roadmap

- PDF text extraction from ASX announcement PDFs (major signal quality boost)
- React dashboard frontend
- Outreach message composition (Jess Style per the playbook)
- Follow-up cadence tracking (Day 0, 4, 10, 21)
- LinkedIn integration for buyer identification
- WebSocket live updates during refresh/enrichment
- Historical trend analysis (signal changes over time)
