"""
Leaderboard DQ filter integration test.

Inserts 3 mock prospects directly into the DB, calls the live API, and
asserts that disqualified rows are excluded from the top-prospects
leaderboard when exclude_dq=true is passed.

Requires:
  - Local PostgreSQL running (same dev credentials as other tests)
  - API running at http://localhost:8000

Mock prospects:
  ZZTL1 — status=enriched,     score=50  → should appear (rank 2)
  ZZTL2 — status=disqualified, score=100 → should NOT appear
  ZZTL3 — status=qualified,    score=75  → should appear (rank 1)

Expected leaderboard order (score DESC): ZZTL3(75) then ZZTL1(50).
ZZTL2(100) excluded despite having the highest score.
"""

import os
import sys
import uuid
import pytest
import psycopg2
import requests
from psycopg2.extras import RealDictCursor

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 5432)),
    "dbname":   os.getenv("DB_NAME", "delta_prospect"),
    "user":     os.getenv("DB_USER", "delta"),
    "password": os.getenv("DB_PASSWORD", "delta_dev"),
}

API_BASE = os.getenv("API_BASE", "http://localhost:8000")

TEST_TICKERS = ["ZZTL1", "ZZTL2", "ZZTL3"]


@pytest.fixture(autouse=True)
def cleanup_test_rows():
    """Delete test rows before and after each test."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM asx_listings WHERE ticker = ANY(%s)", (TEST_TICKERS,))
        conn.commit()
    finally:
        conn.close()

    yield

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM asx_listings WHERE ticker = ANY(%s)", (TEST_TICKERS,))
        conn.commit()
    finally:
        conn.close()


def _insert_mock_prospect(cur, ticker: str, status: str, score: float) -> str:
    """Insert a fake listing + prospect row. Returns prospect_matrix UUID."""
    listing_id = str(uuid.uuid4())
    prospect_id = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO asx_listings
            (id, ticker, company_name, gics_industry_group, gics_sector,
             is_target_sector, market_cap_aud, is_active)
        VALUES (%s, %s, %s, 'Materials', 'Materials', TRUE, 6000000000, TRUE)
    """, (listing_id, ticker, f"Test Leaderboard Co {ticker}"))
    cur.execute("""
        INSERT INTO prospect_matrix
            (id, listing_id, status, prospect_score, status_changed_by)
        VALUES (%s, %s, %s::prospect_status, %s, 'test')
    """, (prospect_id, listing_id, status, score))
    return prospect_id


def test_leaderboard_excludes_dq_when_flag_set():
    """exclude_dq=true: DQ'd row must not appear even with the highest score."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _insert_mock_prospect(cur, "ZZTL1", "enriched",      50.0)
            _insert_mock_prospect(cur, "ZZTL2", "disqualified",  100.0)  # highest score, DQ'd
            _insert_mock_prospect(cur, "ZZTL3", "qualified",     75.0)
        conn.commit()
    finally:
        conn.close()

    r = requests.get(
        f"{API_BASE}/api/prospects",
        params={"limit": 10, "sort_by": "prospect_score", "sort_dir": "desc", "exclude_dq": "true"},
        timeout=10,
    )
    assert r.status_code == 200, f"API returned {r.status_code}: {r.text}"

    body = r.json()
    rows = body.get("data") or body.get("prospects") or body or []

    tickers_in_result = [p["ticker"] for p in rows]

    assert "ZZTL2" not in tickers_in_result, (
        f"ZZTL2 (disqualified, score=100) should be excluded but appeared in: {tickers_in_result}"
    )
    assert "ZZTL1" in tickers_in_result, (
        f"ZZTL1 (enriched, score=50) should be present but missing from: {tickers_in_result}"
    )
    assert "ZZTL3" in tickers_in_result, (
        f"ZZTL3 (qualified, score=75) should be present but missing from: {tickers_in_result}"
    )

    # Confirm score order: ZZTL3(75) before ZZTL1(50)
    idx1 = tickers_in_result.index("ZZTL1")
    idx3 = tickers_in_result.index("ZZTL3")
    assert idx3 < idx1, (
        f"ZZTL3 (score=75) should rank above ZZTL1 (score=50), "
        f"but got positions {idx3} and {idx1}"
    )


def test_leaderboard_includes_dq_when_flag_absent():
    """Without exclude_dq, DQ'd row DOES appear (existing behavior unchanged)."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _insert_mock_prospect(cur, "ZZTL1", "enriched",      50.0)
            _insert_mock_prospect(cur, "ZZTL2", "disqualified",  100.0)
            _insert_mock_prospect(cur, "ZZTL3", "qualified",     75.0)
        conn.commit()
    finally:
        conn.close()

    r = requests.get(
        f"{API_BASE}/api/prospects",
        params={"limit": 10, "sort_by": "prospect_score", "sort_dir": "desc"},
        timeout=10,
    )
    assert r.status_code == 200

    body = r.json()
    rows = body.get("data") or body.get("prospects") or body or []
    tickers_in_result = [p["ticker"] for p in rows]

    assert "ZZTL2" in tickers_in_result, (
        "Without exclude_dq, disqualified rows should still be returned (Lead Matrix behaviour)"
    )
