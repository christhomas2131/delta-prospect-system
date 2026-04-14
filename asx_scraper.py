"""
ASX Listings Scraper & Sector Filter
=====================================
Fetches the full ASX listed companies CSV, maps GICS sectors,
filters to target sectors, and upserts into PostgreSQL.

Data source: https://www.asx.com.au/asx/research/ASXListedCompanies.csv
CSV format:
    Row 1: "ASX listed companies as at <date>"   (metadata — skip)
    Row 2: "Company name,ASX code,GICS industry group"  (header — skip)
    Row 3+: data rows

Usage:
    python asx_scraper.py --mode full
    python asx_scraper.py --mode single --ticker BHP
"""

import csv, io, logging, argparse, sys, os
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field

import httpx
import psycopg2
from psycopg2.extras import execute_values, RealDictCursor

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ASX_CSV_URL = "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"
ASX_COMPANY_URL = "https://www.asx.com.au/asx/1/company/{ticker}"

HTTP_TIMEOUT = 30
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

def _parse_database_url(url: str) -> dict:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "dbname": (parsed.path or "/delta_prospect").lstrip("/"),
        "user": parsed.username or "delta",
        "password": parsed.password or "delta_dev",
    }

_database_url = os.getenv("DATABASE_URL")
DB_CONFIG = _parse_database_url(_database_url) if _database_url else {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME", "delta_prospect"),
    "user":     os.getenv("DB_USER", "delta"),
    "password": os.getenv("DB_PASSWORD", "delta_dev"),
}

# GICS industry groups we consider "target" for Delta's prospecting
TARGET_INDUSTRY_GROUPS = {
    "Energy",
    "Materials",
    "Capital Goods",
    "Utilities",
}

# Industry group → GICS sector mapping
INDUSTRY_TO_SECTOR = {
    "Energy": "Energy",
    "Materials": "Materials",
    "Capital Goods": "Industrials",
    "Commercial & Professional Services": "Industrials",
    "Transportation": "Industrials",
    "Utilities": "Utilities",
    "Automobiles & Components": "Consumer Discretionary",
    "Consumer Discretionary Distribution & Retail": "Consumer Discretionary",
    "Consumer Durables & Apparel": "Consumer Discretionary",
    "Consumer Services": "Consumer Discretionary",
    "Media & Entertainment": "Communication Services",
    "Telecommunication Services": "Communication Services",
    "Consumer Staples Distribution & Retail": "Consumer Staples",
    "Food, Beverage & Tobacco": "Consumer Staples",
    "Household & Personal Products": "Consumer Staples",
    "Banks": "Financials",
    "Diversified Financials": "Financials",
    "Financial Services": "Financials",
    "Insurance": "Financials",
    "Health Care Equipment & Services": "Health Care",
    "Pharmaceuticals, Biotechnology & Life Sciences": "Health Care",
    "Software & Services": "Information Technology",
    "Technology Hardware & Equipment": "Information Technology",
    "Semiconductors & Semiconductor Equipment": "Information Technology",
    "Equity Real Estate Investment Trusts (REITs)": "Real Estate",
    "Real Estate Management & Development": "Real Estate",
    "Not Applic": "Other",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("asx_scraper")

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ASXListing:
    ticker: str
    company_name: str
    gics_industry_group: str
    gics_sector: str = ""
    is_target_sector: bool = False

    def __post_init__(self):
        self.ticker = self.ticker.strip().upper()
        self.company_name = self.company_name.strip()
        self.gics_industry_group = self.gics_industry_group.strip()
        self.gics_sector = INDUSTRY_TO_SECTOR.get(self.gics_industry_group, "Unknown")
        self.is_target_sector = self.gics_industry_group in TARGET_INDUSTRY_GROUPS


@dataclass
class RefreshStats:
    total_parsed: int = 0
    new_listings: int = 0
    updated_listings: int = 0
    delisted_count: int = 0
    target_sector_count: int = 0
    errors: list = field(default_factory=list)

# ---------------------------------------------------------------------------
# CSV fetch & parse
# ---------------------------------------------------------------------------

def fetch_asx_csv(client: httpx.Client) -> str:
    logger.info(f"Fetching ASX CSV from {ASX_CSV_URL}")
    resp = client.get(ASX_CSV_URL, follow_redirects=True)
    resp.raise_for_status()
    text = resp.text.lstrip("\ufeff")
    logger.info(f"Fetched {len(text):,} bytes")
    return text


def parse_asx_csv(csv_text: str) -> list[ASXListing]:
    lines = csv_text.strip().splitlines()
    if len(lines) < 3:
        raise ValueError(f"CSV too short ({len(lines)} lines)")

    logger.info(f"CSV header: {lines[0].strip()}")

    reader = csv.reader(io.StringIO("\n".join(lines[2:])))
    listings, skipped = [], 0

    for row in reader:
        if len(row) < 3 or not row[0].strip() or not row[1].strip():
            skipped += 1
            continue
        listings.append(ASXListing(
            ticker=row[1],
            company_name=row[0],
            gics_industry_group=row[2],
        ))

    logger.info(f"Parsed {len(listings):,} listings ({skipped} skipped)")
    return listings

# ---------------------------------------------------------------------------
# ASX JSON API (single-company enrichment)
# ---------------------------------------------------------------------------

def fetch_company_detail(client: httpx.Client, ticker: str) -> Optional[dict]:
    try:
        resp = client.get(
            ASX_COMPANY_URL.format(ticker=ticker),
            follow_redirects=True,
        )
        if resp.status_code != 200:
            logger.warning(f"ASX API {resp.status_code} for {ticker}")
            return None

        d = resp.json()
        ps = d.get("primary_share", {}) or {}
        return {
            "listing_date": (d.get("listing_date") or "")[:10] or None,
            "website": d.get("web_address"),
            "principal_activities": d.get("principal_activities"),
            "market_cap_aud": int(float(ps["market_cap"]) * 100) if ps.get("market_cap") else None,
            "last_price_aud": int(float(ps["last_price"]) * 100) if ps.get("last_price") else None,
        }
    except Exception as e:
        logger.error(f"Detail fetch failed for {ticker}: {e}")
        return None

# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def upsert_listings(conn, listings: list[ASXListing]) -> RefreshStats:
    stats = RefreshStats(total_parsed=len(listings))
    now = datetime.now(timezone.utc)

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Current active tickers (for delisting detection)
        cur.execute("SELECT ticker FROM asx_listings WHERE is_active = TRUE")
        existing = {r["ticker"] for r in cur.fetchall()}

        # Batch upsert
        values = [
            (l.ticker, l.company_name, l.gics_industry_group,
             l.gics_sector, l.is_target_sector, now)
            for l in listings
        ]
        execute_values(
            cur,
            """
            INSERT INTO asx_listings
                (ticker, company_name, gics_industry_group,
                 gics_sector, is_target_sector, last_refreshed_at)
            VALUES %s
            ON CONFLICT (ticker) DO UPDATE SET
                company_name        = EXCLUDED.company_name,
                gics_industry_group = EXCLUDED.gics_industry_group,
                gics_sector         = EXCLUDED.gics_sector,
                is_target_sector    = EXCLUDED.is_target_sector,
                last_refreshed_at   = EXCLUDED.last_refreshed_at,
                is_active           = TRUE,
                delisted_at         = NULL
            """,
            values,
            template="(%s, %s, %s, %s, %s, %s)",
        )

        current = {l.ticker for l in listings}
        stats.new_listings = len(current - existing)
        stats.updated_listings = len(current & existing)
        stats.target_sector_count = sum(1 for l in listings if l.is_target_sector)

        # Mark delistings
        gone = existing - current
        if gone:
            cur.execute("""
                UPDATE asx_listings
                SET is_active = FALSE, delisted_at = %s
                WHERE ticker = ANY(%s) AND is_active = TRUE
            """, (now, list(gone)))
            stats.delisted_count = cur.rowcount

        # Auto-create prospect_matrix rows for new target-sector companies
        if current - existing:
            cur.execute("""
                INSERT INTO prospect_matrix (listing_id, status, status_changed_by)
                SELECT l.id, 'unscreened', 'system'
                FROM asx_listings l
                WHERE l.ticker = ANY(%s)
                  AND l.is_target_sector = TRUE
                  AND NOT EXISTS (
                      SELECT 1 FROM prospect_matrix pm WHERE pm.listing_id = l.id
                  )
            """, (list(current - existing),))
            logger.info(f"Created {cur.rowcount} new prospect rows")

        conn.commit()
    return stats


def backfill_prospect_matrix(conn) -> int:
    """Ensure every active target-sector listing has a prospect row."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO prospect_matrix (listing_id, status, status_changed_by)
            SELECT l.id, 'unscreened', 'system'
            FROM asx_listings l
            WHERE l.is_target_sector = TRUE
              AND l.is_active = TRUE
              AND NOT EXISTS (
                  SELECT 1 FROM prospect_matrix pm WHERE pm.listing_id = l.id
              )
        """)
        n = cur.rowcount
        conn.commit()
        if n:
            logger.info(f"Backfilled {n} prospect rows")
        return n


def record_refresh(conn, run_type: str, stats: RefreshStats, triggered_by: str) -> str:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO refresh_runs
                (run_type, total_listings, new_listings, updated_listings,
                 delisted_count, target_sector_count, status, completed_at, triggered_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
            RETURNING id
        """, (
            run_type, stats.total_parsed, stats.new_listings, stats.updated_listings,
            stats.delisted_count, stats.target_sector_count,
            "failed" if stats.errors else "completed", triggered_by,
        ))
        rid = str(cur.fetchone()[0])
        conn.commit()
    return rid

# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_full_refresh(triggered_by: str = "system"):
    logger.info("=" * 60)
    logger.info("FULL ASX REFRESH — START")
    logger.info("=" * 60)

    with httpx.Client(headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT) as client:
        csv_text = fetch_asx_csv(client)
        listings = parse_asx_csv(csv_text)

    if len(listings) < 1500:
        logger.warning(f"Only {len(listings)} listings — expected ~2400. Possible truncation.")

    conn = get_conn()
    try:
        stats = upsert_listings(conn, listings)
        backfill_prospect_matrix(conn)
        rid = record_refresh(conn, "weekly", stats, triggered_by)

        logger.info("=" * 60)
        logger.info("REFRESH COMPLETE")
        logger.info(f"  Run ID:       {rid}")
        logger.info(f"  Total:        {stats.total_parsed:,}")
        logger.info(f"  New:          {stats.new_listings:,}")
        logger.info(f"  Updated:      {stats.updated_listings:,}")
        logger.info(f"  Delisted:     {stats.delisted_count:,}")
        logger.info(f"  Target sect:  {stats.target_sector_count:,}")
        logger.info("=" * 60)
        return stats
    finally:
        conn.close()


def run_single_refresh(ticker: str, triggered_by: str = "manual"):
    ticker = ticker.upper()
    logger.info(f"Single refresh: {ticker}")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM asx_listings WHERE ticker=%s", (ticker,))
            row = cur.fetchone()
            if not row:
                logger.error(f"{ticker} not in DB. Run full refresh first.")
                return None

        with httpx.Client(headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT) as client:
            detail = fetch_company_detail(client, ticker)

        if not detail:
            logger.warning(f"No detail for {ticker}")
            return None

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE asx_listings SET
                    listing_date=%s, website=%s, principal_activities=%s,
                    market_cap_aud=%s, last_price_aud=%s, last_refreshed_at=NOW()
                WHERE ticker=%s
            """, (
                detail["listing_date"], detail["website"],
                detail["principal_activities"],
                detail["market_cap_aud"], detail["last_price_aud"], ticker,
            ))
            cur.execute("""
                INSERT INTO enrichment_log
                    (listing_id, action, source_type, success, documents_processed, triggered_by)
                SELECT id, 'company_info_pull', 'asx_announcement', TRUE, 1, %s
                FROM asx_listings WHERE ticker=%s
            """, (triggered_by, ticker))
            conn.commit()
        logger.info(f"{ticker} updated")
        return detail
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="ASX Scraper — Delta Prospect System")
    p.add_argument("--mode", choices=["full", "single"], required=True)
    p.add_argument("--ticker", type=str, help="Required for --mode single")
    p.add_argument("--triggered-by", type=str, default="manual")
    args = p.parse_args()

    if args.mode == "single" and not args.ticker:
        p.error("--ticker required for single mode")

    try:
        if args.mode == "full":
            run_full_refresh(args.triggered_by)
        else:
            run_single_refresh(args.ticker, args.triggered_by)
    except Exception as e:
        logger.exception(f"Scraper failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
