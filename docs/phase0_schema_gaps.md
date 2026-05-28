# Phase 0 Auto-DQ — Schema Gap Documentation

**Produced:** 2026-05-28
**Context:** Phase 0 Gate 1 is live. Gates 2 and 3 are blocked pending the schema additions
documented here. This file is the permanent record of what the Phase 1 dry-run found.

---

## What is implemented (Gate 1)

Gate 1 is live in `asx_scraper.py::apply_gate1_dq()` and ran retroactively against
`prospect_matrix` on 2026-05-28 (Phase 2 sweep DQ'd 41 prospects).

**Rule:** `market_cap_aud > 0 AND market_cap_aud < 500,000,000` (cents = AUD $5M)

`market_cap_aud` is stored on `asx_listings` in **cents** (integer). Rows where
`market_cap_aud = 0` are excluded — they represent stale/unrefreshed data, not genuinely
zero market cap companies.

---

## What is NOT implemented (Gates 2 and 3)

### Gate 2 — Zero-volume auto-DQ

**Rule (intended):** `volume_last_session = 0 AND market_cap_aud < 50,000,000`

**Status: BLOCKED — column does not exist.**

| Item | Detail |
|---|---|
| Missing column | `volume_last_session` (or `day_volume`) on `asx_listings` |
| Missing column type | INTEGER — count of shares traded in the last session |
| Schema migration needed | `ALTER TABLE asx_listings ADD COLUMN day_volume INTEGER;` |
| Data source | ASX Company Header API (`/asx/1/share/{ticker}`) — field exists in the API response payload but is **not currently extracted or stored** by the scraper |
| Scraper change needed | In `fetch_company_detail()` in `asx_scraper.py`: add `"day_volume": int(header.get("dayVolume", 0) or 0)` to the returned dict, and add `day_volume = COALESCE(%s, day_volume)` to the `UPDATE asx_listings` statement in `update_company_detail()` |

**Why Gate 2 matters:** Some prospects survive Gate 1 (market cap technically above $5M) but
are effectively dead — no shares traded for days or weeks, market cap between $5M and $50M,
no operating business. Gate 2 catches these. The manually DQ'd ORP ($20.8M) and FZR ($19.4M)
are the canonical examples — both would have been caught by Gate 2 if volume data were available.

**Caveat on data freshness:** `volume_last_session` will be 0 on weekends and public holidays
for every company. The gate logic should guard against this — for example, only apply Gate 2
if `last_refreshed_at` is a trading day, or check `volume_last_session = 0` over N consecutive
scrape runs rather than a single snapshot.

---

### Gate 3 — Manual review flag (small operator)

**Rule (intended):** `market_cap_aud < 50,000,000 AND ttm_revenue_usd < 10,000,000`
→ sets `manual_review_required = TRUE` (does NOT auto-DQ, status stays `qualified`)

**Status: BLOCKED — column and data source both missing.**

| Item | Detail |
|---|---|
| Missing column (DB) | `ttm_revenue_usd` on `asx_listings` or `prospect_matrix` |
| Missing column type | `BIGINT` (store in USD cents, consistent with `market_cap_aud`) or `NUMERIC(18,2)` in USD dollars — choose one and document it |
| Schema migration needed | `ALTER TABLE asx_listings ADD COLUMN ttm_revenue_usd_cents BIGINT;` |
| Data source | **Not available from ASX CSV or Company Header/About APIs.** Revenue is a financial statement line item. The ASX APIs used by the scraper return price, market cap, and company description — not income statement data. |
| Flag column | `manual_review_required BOOLEAN DEFAULT FALSE` — **already added to `prospect_matrix`** as part of the Phase 2 migration (`ALTER TABLE` in `phase2_gate1.sql`, 2026-05-28) |

**Why Gate 3 matters:** Some prospects are small-but-real operators that don't belong in the
auto-DQ set but do need a human look before outreach. ANO (Advance ZincTek, $50.1M AUD,
$8.1M USD TTM revenue) is the boundary case — it passes Gate 3 cleanly by both metrics,
but a company at $45M AUD cap and $4M revenue should be reviewed rather than auto-qualified.

**Revenue data source options (ranked by effort):**

1. **`prospect_intelligence_runs.output_json`** — the deep analysis agent already pulls
   ASX announcements and quarterly reports. If the output JSON were structured to include
   a `ttm_revenue_usd` field, Gate 3 could run as a post-enrichment pass rather than a
   scraper-time check. Low new infrastructure, but only works for enriched prospects.

2. **ASX quarterly/annual report parsing** — the enrichment agent fetches announcement
   titles. Revenue figures sometimes appear in quarterly activities report titles
   (e.g. "Revenue up 30% to $X.XM"). A pattern-match extraction could seed a rough
   `ttm_revenue_usd` for enriched companies. Fragile but free.

3. **External financial data API** — a dedicated financial data provider
   (e.g. Simplywall.st API, Morningstar, or a market data vendor) would give clean
   structured revenue. Adds cost and a new dependency.

4. **Manual entry** — `ttm_revenue_usd` could be populated manually for the ~50 prospects
   that pass Gate 1 and Gate 2 and reach the review queue. Small volume, no infra needed.

---

## Summary table

| Gate | Rule | Status | Blocking item |
|---|---|---|---|
| Gate 1 | `market_cap_aud < AUD $5M` | ✅ Live | — |
| Gate 2 | `volume = 0 AND market_cap < AUD $50M` | ❌ Blocked | `day_volume` column + scraper extraction |
| Gate 3 | `market_cap < AUD $50M AND revenue < USD $10M` → manual review | ❌ Blocked | `ttm_revenue_usd` column + data source |

---

## Schema migrations required to unblock Gates 2 and 3

```sql
-- Gate 2: add trading volume to asx_listings
ALTER TABLE asx_listings ADD COLUMN IF NOT EXISTS day_volume INTEGER;

-- Gate 3: add TTM revenue to asx_listings
-- Stored in USD cents (consistent with market_cap_aud in AUD cents)
ALTER TABLE asx_listings ADD COLUMN IF NOT EXISTS ttm_revenue_usd_cents BIGINT;

-- Gate 3 flag column: already exists on prospect_matrix (added 2026-05-28)
-- ALTER TABLE prospect_matrix ADD COLUMN IF NOT EXISTS manual_review_required BOOLEAN DEFAULT FALSE;
```

---

## Scraper changes required to populate the new columns

Both columns need to be wired into `fetch_company_detail()` and `update_company_detail()`
in `asx_scraper.py`.

**`fetch_company_detail()` return dict additions:**
```python
# Gate 2 — day volume from ASX header API
"day_volume": int(float(header.get("dayVolume") or 0)),

# Gate 3 — revenue not available from ASX header/about APIs; leave as None
# "ttm_revenue_usd_cents": None,
```

**`update_company_detail()` SQL additions:**
```sql
-- In the UPDATE asx_listings SET ... statement:
day_volume = COALESCE(%s, day_volume),
```

**`apply_gate1_dq()` analogs to write:**
```python
def apply_gate2_dq(conn) -> int:
    """Gate 2: zero-volume + market cap < AUD $50M."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE prospect_matrix pm
            SET
                status    = 'disqualified',
                dq_reason = 'Phase 0 Gate 2: zero trading volume + market cap AUD below $50M floor (auto-DQ)'
            FROM asx_listings l
            WHERE l.id = pm.listing_id
              AND l.day_volume = 0
              AND l.market_cap_aud > 0
              AND l.market_cap_aud < 5000000000   -- AUD $50M in cents
              AND l.is_active = TRUE
              AND pm.status NOT IN ('disqualified', 'archived')
        """)
        n = cur.rowcount
        conn.commit()
    if n:
        logger.info(f"Phase 0 Gate 2: auto-DQ'd {n} prospect(s) (zero volume + cap < AUD $50M)")
    return n


def apply_gate3_flag(conn) -> int:
    """Gate 3: small operator flag for manual review (does not DQ)."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE prospect_matrix pm
            SET manual_review_required = TRUE
            FROM asx_listings l
            WHERE l.id = pm.listing_id
              AND l.market_cap_aud > 0
              AND l.market_cap_aud < 5000000000         -- AUD $50M in cents
              AND l.ttm_revenue_usd_cents IS NOT NULL
              AND l.ttm_revenue_usd_cents < 1000000000  -- USD $10M in cents
              AND l.is_active = TRUE
              AND pm.status NOT IN ('disqualified', 'archived')
              AND pm.manual_review_required = FALSE
        """)
        n = cur.rowcount
        conn.commit()
    if n:
        logger.info(f"Phase 0 Gate 3: flagged {n} prospect(s) for manual review")
    return n
```

These functions are ready to be added to `asx_scraper.py` and wired into `run_full_refresh()`
once the schema migrations above have been run and the scraper is populating the new columns.

---

## Verification queries (run after migration + scraper update)

```sql
-- Confirm day_volume is being populated
SELECT ticker, day_volume, market_cap_aud / 100.0 / 1000000 AS cap_aud_m
FROM asx_listings
WHERE is_active = TRUE AND is_target_sector = TRUE
ORDER BY day_volume ASC NULLS FIRST
LIMIT 20;

-- Gate 2 dry-run
SELECT l.ticker, l.company_name,
       l.day_volume,
       ROUND(l.market_cap_aud / 100.0 / 1000000, 2) AS cap_aud_m,
       pm.status
FROM prospect_matrix pm
JOIN asx_listings l ON l.id = pm.listing_id
WHERE l.day_volume = 0
  AND l.market_cap_aud > 0
  AND l.market_cap_aud < 5000000000
  AND l.is_active = TRUE
  AND pm.status NOT IN ('disqualified', 'archived')
ORDER BY l.market_cap_aud ASC;

-- Gate 3 dry-run
SELECT l.ticker, l.company_name,
       ROUND(l.market_cap_aud / 100.0 / 1000000, 2) AS cap_aud_m,
       ROUND(l.ttm_revenue_usd_cents / 100.0 / 1000000, 2) AS revenue_usd_m,
       pm.status
FROM prospect_matrix pm
JOIN asx_listings l ON l.id = pm.listing_id
WHERE l.market_cap_aud > 0
  AND l.market_cap_aud < 5000000000
  AND l.ttm_revenue_usd_cents IS NOT NULL
  AND l.ttm_revenue_usd_cents < 1000000000
  AND l.is_active = TRUE
  AND pm.status NOT IN ('disqualified', 'archived')
ORDER BY l.market_cap_aud ASC;
```
