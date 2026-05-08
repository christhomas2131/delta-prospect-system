#!/usr/bin/env python3
"""
load_snapshot.py — Load a PitchBook JSON snapshot into the prospect DB.

Usage:
    python load_snapshot.py bci_snapshot.json BCI
    python load_snapshot.py rio_snapshot.json RIO
    python load_snapshot.py bhp_snapshot.json BHP

JSON file must contain:
    {
      "snapshot_data": { ... },       # arbitrary dict — financials, contacts, etc.
      "snapshot_markdown": "..."      # formatted text to render in the dashboard
    }
"""

import argparse
import json
import os
import sys

import psycopg2
from psycopg2.extras import RealDictCursor, Json
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# DB config — identical pattern to enrichment_agent.py
# ---------------------------------------------------------------------------

def _parse_database_url(url: str) -> dict:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return {
        "host":     parsed.hostname or "localhost",
        "port":     parsed.port or 5432,
        "dbname":   (parsed.path or "/delta_prospect").lstrip("/"),
        "user":     parsed.username or "delta",
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

API_BASE = f"http://localhost:{os.getenv('PORT', os.getenv('APP_PORT', '8000'))}"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Load a PitchBook snapshot JSON into pitchbook_snapshots")
    parser.add_argument("json_file", help="Path to snapshot JSON (e.g. bci_snapshot.json)")
    parser.add_argument("ticker",    help="ASX ticker symbol (e.g. BCI)")
    args = parser.parse_args()

    ticker = args.ticker.upper()

    # 1. Read JSON file — also check snapshots/ if not found at given path
    json_path = args.json_file
    if not os.path.isfile(json_path):
        fallback = os.path.join(os.path.dirname(__file__), "snapshots", os.path.basename(json_path))
        if os.path.isfile(fallback):
            json_path = fallback
        else:
            print(f"ERROR: File not found: {json_path}", file=sys.stderr)
            sys.exit(1)
    args.json_file = json_path

    with open(args.json_file, encoding="utf-8") as f:
        payload = json.load(f)

    snapshot_data     = payload.get("snapshot_data", {})
    snapshot_markdown = payload.get("snapshot_markdown", "")

    if not snapshot_data and not snapshot_markdown:
        print("ERROR: JSON must contain snapshot_data and/or snapshot_markdown.", file=sys.stderr)
        sys.exit(1)

    # 2. Connect and look up prospect_id
    try:
        conn = psycopg2.connect(**DB_CONFIG)
    except psycopg2.OperationalError as e:
        print(f"ERROR: Cannot connect to database — {e}", file=sys.stderr)
        sys.exit(1)

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT pm.id AS prospect_id
                FROM prospect_matrix pm
                JOIN asx_listings l ON l.id = pm.listing_id
                WHERE l.ticker = %s
                  AND l.is_active = TRUE
                LIMIT 1
            """, (ticker,))
            row = cur.fetchone()

        if not row:
            print(
                f"ERROR: No active prospect_matrix row found for ticker '{ticker}'.\n"
                f"       Run the scraper first: python asx_scraper.py --mode single --ticker {ticker}",
                file=sys.stderr,
            )
            sys.exit(1)

        prospect_id = str(row["prospect_id"])

        # 3. UPSERT — xmax=0 means the row was just inserted (not updated)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pitchbook_snapshots
                    (prospect_id, snapshot_data, snapshot_markdown, enriched_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (prospect_id) DO UPDATE SET
                    snapshot_data     = EXCLUDED.snapshot_data,
                    snapshot_markdown = EXCLUDED.snapshot_markdown,
                    enriched_at       = NOW()
                RETURNING (xmax = 0) AS inserted
            """, (prospect_id, Json(snapshot_data), snapshot_markdown))
            action = "inserted" if cur.fetchone()[0] else "updated"
        conn.commit()

    finally:
        conn.close()

    # 4. Confirmation
    print(f"\n  Ticker       : {ticker}")
    print(f"  Prospect ID  : {prospect_id}")
    print(f"  Action       : {action}")
    print(f"  Markdown len : {len(snapshot_markdown):,} chars")

    # 5. Verify via FastAPI endpoint
    url = f"{API_BASE}/api/prospects/{prospect_id}/snapshot"
    print(f"\nVerifying: GET {url}")
    try:
        import httpx
        resp = httpx.get(url, timeout=10.0)
        print(f"  HTTP {resp.status_code}")
        if resp.status_code == 200:
            body = resp.json()
            print(f"  prospect_id  : {body.get('prospect_id')}")
            print(f"  enriched_at  : {body.get('enriched_at')}")
            print(f"  enriched_by  : {body.get('enriched_by')}")
            preview = (body.get("snapshot_markdown") or "")[:120].replace("\n", " ")
            print(f"  md preview   : {preview}...")
        else:
            print(f"  Response     : {resp.text[:200]}")
    except Exception as e:
        print(f"  WARNING: API check failed — {e}")
        print(f"  (Is the API server running on {API_BASE}?)")


if __name__ == "__main__":
    main()
