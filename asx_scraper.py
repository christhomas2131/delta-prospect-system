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

import csv, io, logging, argparse, sys, os, re, time
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
ASX_COMPANY_ABOUT_URL = "https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/companies/{ticker}/about?v=undefined"
ASX_COMPANY_HEADER_URL = "https://asx.api.markitdigital.com/asx-research/1.0/companies/{ticker}/header"
MARKIT_TOKEN = "83ff96335c2d45a094df02a206a39ff4"

HTTP_TIMEOUT = 30
DETAIL_REQUEST_DELAY = 0.35
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

MARKIT_API_HEADERS = {
    **HTTP_HEADERS,
    "Authorization": f"Bearer {MARKIT_TOKEN}",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.asx.com.au/",
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

AU_STATE_ALIASES = {
    "NSW": "NSW",
    "NEW SOUTH WALES": "NSW",
    "VIC": "VIC",
    "VICTORIA": "VIC",
    "QLD": "QLD",
    "QUEENSLAND": "QLD",
    "WA": "WA",
    "WESTERN AUSTRALIA": "WA",
    "SA": "SA",
    "SOUTH AUSTRALIA": "SA",
    "TAS": "TAS",
    "TASMANIA": "TAS",
    "ACT": "ACT",
    "AUSTRALIAN CAPITAL TERRITORY": "ACT",
    "NT": "NT",
    "NORTHERN TERRITORY": "NT",
}

NON_AU_COUNTRY_HINTS = {
    "NEW ZEALAND", "SINGAPORE", "UNITED STATES", "USA", "UNITED KINGDOM", "UK",
    "CANADA", "HONG KONG", "CHINA", "JAPAN",
}

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


def _clean_text(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _normalize_state(value: str) -> Optional[str]:
    if not value:
        return None
    key = _clean_text(value).upper().strip(",")
    return AU_STATE_ALIASES.get(key)


def _extract_address_from_text(text: str) -> Optional[dict]:
    text = _clean_text(text)
    if not text:
        return None

    m = re.search(
        r"(?P<city>[A-Za-z][A-Za-z .'\-]{1,60}?)[,\s]+"
        r"(?P<state>NSW|VIC|QLD|WA|SA|TAS|ACT|NT|"
        r"New South Wales|Victoria|Queensland|Western Australia|South Australia|Tasmania|Australian Capital Territory|Northern Territory)"
        r"(?:,\s*AUSTRALIA)?(?:,\s*|\s+)(?P<postcode>\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if m:
        city = _clean_text(m.group("city")).title()
        state = _normalize_state(m.group("state"))
        country = "Australia"
        return {
            "raw_address": text,
            "city": city,
            "state": state,
            "country": country,
            "in_australia": True,
            "confidence": 0.92,
        }

    upper_text = text.upper()
    for country in NON_AU_COUNTRY_HINTS:
        if country in upper_text:
            return {
                "raw_address": text,
                "city": None,
                "state": None,
                "country": country.title(),
                "in_australia": False,
                "confidence": 0.65,
            }

    if "AUSTRALIA" in upper_text:
        return {
            "raw_address": text,
            "city": None,
            "state": None,
            "country": "Australia",
            "in_australia": True,
            "confidence": 0.55,
        }

    return None


def _collect_address_candidates(value, path="root") -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if isinstance(value, dict):
        # Prefer explicit structured address/location objects when present.
        likely_keys = (
            "registered_office", "registered_office_address", "office_address",
            "principal_place_of_business", "principal_place_of_business_address",
            "address", "addresses", "head_office", "location", "contact",
        )
        joined_parts = []
        if any(k in path.lower() for k in likely_keys):
            for k, v in value.items():
                if isinstance(v, (str, int, float)):
                    joined_parts.append(_clean_text(v))
            joined = ", ".join(part for part in joined_parts if part)
            if joined:
                candidates.append((path, joined))

        for k, v in value.items():
            next_path = f"{path}.{k}"
            if isinstance(v, (dict, list)):
                candidates.extend(_collect_address_candidates(v, next_path))
            elif isinstance(v, str):
                lower_key = k.lower()
                if any(token in lower_key for token in ("address", "office", "location", "place", "country", "city", "state")):
                    text = _clean_text(v)
                    if text:
                        candidates.append((next_path, text))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            candidates.extend(_collect_address_candidates(item, f"{path}[{idx}]"))
    return candidates


def extract_location_from_company_payload(payload: dict) -> dict:
    default = {
        "raw_address": None,
        "city": None,
        "state": None,
        "country": None,
        "in_australia": None,
        "source": None,
        "confidence": None,
    }

    # First pass: structured object keys if ASX exposes them.
    for key in (
        "registered_office", "registered_office_address", "office_address",
        "principal_place_of_business", "principal_place_of_business_address",
        "address", "head_office", "location",
    ):
        val = payload.get(key)
        if isinstance(val, dict):
            city = _clean_text(val.get("city") or val.get("suburb")) or None
            state = _normalize_state(val.get("state") or val.get("state_code") or val.get("region"))
            country = _clean_text(val.get("country")) or None
            raw_parts = [
                _clean_text(val.get("line1") or val.get("address_line_1") or val.get("street")),
                _clean_text(val.get("line2") or val.get("address_line_2")),
                city,
                state,
                _clean_text(val.get("postcode") or val.get("postal_code")),
                country,
            ]
            raw_address = ", ".join(part for part in raw_parts if part) or None
            if city or state or country or raw_address:
                in_australia = True if (country or "").strip().lower() == "australia" or state in AU_STATE_ALIASES.values() else None
                return {
                    "raw_address": raw_address,
                    "city": city.title() if city else None,
                    "state": state,
                    "country": country.title() if country else ("Australia" if in_australia else None),
                    "in_australia": in_australia,
                    "source": f"asx_company_api:{key}",
                    "confidence": 0.98 if city and state else 0.88,
                }

    # Second pass: mine address-like text blocks anywhere in the payload.
    for path, candidate in _collect_address_candidates(payload):
        parsed = _extract_address_from_text(candidate)
        if parsed:
            parsed["source"] = f"asx_company_api:{path}"
            return parsed

    # Fallback: principal activities sometimes mentions Australian locality.
    parsed = _extract_address_from_text(payload.get("principal_activities") or "")
    if parsed:
        parsed["source"] = "principal_activities_heuristic"
        parsed["confidence"] = min(parsed["confidence"] or 0.5, 0.5)
        return parsed

    return default

# ---------------------------------------------------------------------------
# ASX JSON API (single-company enrichment)
# ---------------------------------------------------------------------------

def fetch_company_detail(client: httpx.Client, ticker: str) -> Optional[dict]:
    try:
        about_resp = client.get(
            ASX_COMPANY_ABOUT_URL.format(ticker=ticker),
            headers=MARKIT_API_HEADERS,
            follow_redirects=True,
        )
        header_resp = client.get(
            ASX_COMPANY_HEADER_URL.format(ticker=ticker),
            headers=MARKIT_API_HEADERS,
            follow_redirects=True,
        )
        if about_resp.status_code != 200 or header_resp.status_code != 200:
            logger.warning(f"ASX company detail API returned {about_resp.status_code}/{header_resp.status_code} for {ticker}")
            return None

        about = (about_resp.json() or {}).get("data") or {}
        header = (header_resp.json() or {}).get("data") or {}
        location = extract_location_from_company_payload(about)
        return {
            "listing_date": (header.get("dateListed") or "")[:10] or None,
            "website": about.get("websiteUrl"),
            "principal_activities": about.get("description"),
            "market_cap_aud": int(float(header["marketCap"]) * 100) if header.get("marketCap") else None,
            "last_price_aud": int(float(header["priceLast"]) * 100) if header.get("priceLast") else None,
            "registered_address_raw": location["raw_address"],
            "registered_city": location["city"],
            "registered_state": location["state"],
            "registered_country": location["country"],
            "in_australia": location["in_australia"],
            "location_source": location["source"],
            "location_confidence": location["confidence"],
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


def update_company_detail(conn, ticker: str, detail: dict, triggered_by: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE asx_listings SET
                listing_date=%s,
                website=COALESCE(%s, website),
                principal_activities=COALESCE(%s, principal_activities),
                market_cap_aud=COALESCE(%s, market_cap_aud),
                last_price_aud=COALESCE(%s, last_price_aud),
                registered_address_raw=%s,
                registered_city=%s,
                registered_state=%s,
                registered_country=%s,
                location_source=%s,
                location_confidence=%s,
                last_refreshed_at=NOW()
            WHERE ticker=%s
        """, (
            detail["listing_date"],
            detail["website"],
            detail["principal_activities"],
            detail["market_cap_aud"],
            detail["last_price_aud"],
            detail["registered_address_raw"],
            detail["registered_city"],
            detail["registered_state"],
            detail["registered_country"],
            detail["location_source"],
            detail["location_confidence"],
            ticker,
        ))

        cur.execute("""
            UPDATE prospect_matrix pm
            SET
                registered_city = %s,
                registered_state = %s,
                in_australia = COALESCE(%s, FALSE)
            FROM asx_listings l
            WHERE l.id = pm.listing_id
              AND l.ticker = %s
        """, (
            detail["registered_city"],
            detail["registered_state"],
            detail["in_australia"],
            ticker,
        ))

        cur.execute("""
            INSERT INTO enrichment_log
                (listing_id, action, source_type, success, documents_processed, triggered_by)
            SELECT id, 'company_info_pull', 'asx_announcement', TRUE, 1, %s
            FROM asx_listings WHERE ticker=%s
        """, (triggered_by, ticker))
        conn.commit()


def refresh_target_company_details(conn, only_missing_location: bool = True, triggered_by: str = "system") -> int:
    """Backfill target-sector company detail, especially HQ location fields."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        where_clause = """
            WHERE is_active = TRUE
              AND is_target_sector = TRUE
        """
        if only_missing_location:
            where_clause += """
              AND (
                    registered_city IS NULL
                 OR registered_state IS NULL
                 OR location_source IS NULL
              )
            """
        cur.execute(f"""
            SELECT ticker
            FROM asx_listings
            {where_clause}
            ORDER BY ticker
        """)
        rows = cur.fetchall()

    tickers = [r["ticker"] for r in rows]
    if not tickers:
        logger.info("No target-sector companies needed detail backfill")
        return 0

    updated = 0
    logger.info(f"Backfilling company detail for {len(tickers)} target-sector companies")
    with httpx.Client(headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT) as client:
        for i, ticker in enumerate(tickers):
            detail = fetch_company_detail(client, ticker)
            if detail:
                update_company_detail(conn, ticker, detail, triggered_by)
                updated += 1
            if i < len(tickers) - 1:
                time.sleep(DETAIL_REQUEST_DELAY)

    logger.info(f"Company detail backfill complete: {updated}/{len(tickers)} updated")
    return updated


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
        refresh_target_company_details(conn, only_missing_location=True, triggered_by=triggered_by)
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

        update_company_detail(conn, ticker, detail, triggered_by)
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
