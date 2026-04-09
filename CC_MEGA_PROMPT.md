Read CLAUDE.md thoroughly before starting. This is a multi-phase build. Complete each phase fully, verify it works, then move to the next automatically. Do not stop between phases unless something fails. If something fails, fix it and continue. No API keys are needed — the enrichment engine is rule-based and free.

PHASE A — LOCAL SETUP:
1. Create Python venv, activate it, install requirements.txt
2. Spin up PostgreSQL via Docker: docker run -d --name delta-pg -e POSTGRES_USER=delta -e POSTGRES_PASSWORD=delta_dev -e POSTGRES_DB=delta_prospect -p 5432:5432 postgres:15
3. If Docker is not available or the command fails, help me install PostgreSQL another way
4. Wait for postgres to be ready (poll with pg_isready or sleep+retry)
5. Run schema.sql against the database
6. Create .env from .env.example with the Docker credentials
7. Verify: connect to DB, confirm all 6 tables exist, confirm gics_sector_map has rows

PHASE B — ASX DATA LOAD:
1. Run: python asx_scraper.py --mode full
2. Query and show me: total listings, target sector count, prospect matrix rows created
3. Show sector breakdown: SELECT gics_sector, COUNT(*) FROM asx_listings WHERE is_target_sector = TRUE AND is_active = TRUE GROUP BY gics_sector ORDER BY count DESC

PHASE C — ENRICHMENT (FREE, NO API KEY):
1. Run enrichment on a single company first to test: python enrichment_agent.py --mode single --ticker BHP
2. Show me the pressure_signals created for BHP
3. Show me BHP's prospect_matrix row with its score
4. If that worked, run the full batch: python enrichment_agent.py --mode batch
5. Note: batch will take ~20 minutes due to ASX API rate limiting. That's normal. Let it run.
6. After batch completes, show summary stats: how many enriched, average score, top 10 by score

PHASE D — API VERIFICATION:
1. Start uvicorn in the background: uvicorn api:app --host 0.0.0.0 --port 8000 &
2. Test and show responses: GET /api/health, GET /api/stats, GET /api/prospects?limit=5, GET /api/sectors
3. Kill the background uvicorn process after verification

PHASE E — REACT DASHBOARD:
Create a React frontend in a /frontend subfolder using Vite + Tailwind CSS.

Proxy config: all /api requests proxy to http://localhost:8000 via vite.config.

Build these views:

1. DASHBOARD HOME (default route "/")
   - Stats cards: Total Prospects, Enriched, Ready for Outreach, Avg Score (from GET /api/stats)
   - Sector breakdown table with company counts (from GET /api/sectors)
   - "Refresh ASX Data" button that POSTs to /api/refresh and shows a toast/notification

2. PROSPECT MATRIX TABLE (route "/prospects")
   - Data table: Ticker, Company Name, Sector, Status, Score, Signal Count, Dominant Pressure Type
   - Default sort: score descending
   - Clickable column headers to sort
   - Filter dropdowns: Status, Sector (populated from actual data)
   - Search input that filters by company name or ticker
   - Click any row to navigate to detail view
   - Pagination (50 per page)

3. PROSPECT DETAIL VIEW (route "/prospects/:id")
   - Header: company name, ticker, sector, market cap (formatted as $XM or $XB AUD), website link
   - Strategic profile card: direction, tailwind, headwind, likelihood score (as a /10 bar or badge)
   - Pressure signals list: each shows type (color-coded badge), strength (color-coded badge), summary text, confidence %, source date
   - Editable analyst notes textarea with save button (PATCHes /api/prospects/:id)
   - Status change buttons: Qualified, Enriched, Ready for Outreach, Suggested DQ (PATCHes /api/prospects/:id)
   - "Enrich Now" button that POSTs to /api/enrich/{ticker}
   - Back button to return to matrix

4. NAVIGATION
   - Sidebar or top nav with: Dashboard, Prospect Matrix
   - Show app name "Delta Prospect System" in nav

Design: Dark theme. Industrial/utilitarian. Bloomberg terminal meets mining ops dashboard. Sharp edges, no rounded corners on cards. Monospace or technical font for data values. Color coding: operational=orange, cost=red, safety=yellow, governance=purple, environmental=green, market=blue, workforce=teal. Signal strength: strong=bright, moderate=medium, weak=dim. Dense but readable.

After building, start both the API (uvicorn) and the frontend (npm run dev) and confirm the dashboard loads with real data from the database.

PHASE F — FINAL VERIFICATION:
1. Confirm the dashboard shows real prospect data from the ASX load
2. Confirm clicking a prospect shows its detail view with pressure signals
3. Confirm the search works
4. Confirm the sector breakdown shows real numbers
5. Show me the URLs to access both the API and the frontend
6. Give me a summary of everything that's running and how to start/stop it all
