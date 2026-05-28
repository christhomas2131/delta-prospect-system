"""
Phase 0 Gate 1 smoke tests — integration tests against the real dev DB.

Each test runs inside a transaction that is always rolled back, so test
rows never persist. Requires the local PostgreSQL DB to be running with
the standard dev credentials.

BASELINE (before scraper modification):
  test_baseline_gate1_function_absent — apply_gate1_dq does not exist yet.
  test_baseline_new_prospect_status   — new rows inserted as 'unscreened', no DQ applied.

MODIFIED (after scraper modification):
  test_gate1_sub5m_triggers_dq        — market_cap $3M AUD → disqualified.
  test_gate1_zero_cap_skipped         — market_cap $0 (stale) → NOT DQ'd.
  test_gate1_30m_cap_skipped          — market_cap $30M AUD → NOT DQ'd.
  test_gate1_100m_cap_skipped         — market_cap $100M AUD → NOT DQ'd.
  test_gate1_already_dq_untouched     — already disqualified row → NOT overwritten.
"""

import os
import sys
import uuid
import pytest
import psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 5432)),
    "dbname":   os.getenv("DB_NAME", "delta_prospect"),
    "user":     os.getenv("DB_USER", "delta"),
    "password": os.getenv("DB_PASSWORD", "delta_dev"),
}

# Fake tickers that won't collide with real ASX data
TEST_TICKERS = ["ZZT1", "ZZT2", "ZZT3", "ZZT4", "ZZT5"]

# market_cap_aud stored in CENTS
CAP_3M   =   300_000_000   # $3M AUD  — should trigger Gate 1
CAP_0    =             0   # $0       — stale/unknown, should NOT trigger
CAP_30M  = 3_000_000_000   # $30M AUD — should NOT trigger
CAP_100M =10_000_000_000   # $100M AUD — should NOT trigger


@pytest.fixture
def db_conn():
    """Open a real DB connection. Deletes all test ticker rows at teardown.

    We can't rely on ROLLBACK alone because apply_gate1_dq() calls conn.commit()
    internally, which commits any test inserts made in the same transaction.
    Explicit DELETE is the safe cleanup path.
    """
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    yield conn
    # Always clean up test rows — whether the test passed, failed, or committed
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM asx_listings WHERE ticker = ANY(%s)",
            (TEST_TICKERS,)
        )
    conn.commit()
    conn.close()


def _insert_test_prospect(cur, ticker: str, market_cap_cents: int,
                           status: str = "unscreened") -> str:
    """Insert a fake listing + prospect row. Returns the prospect_matrix UUID."""
    listing_id = str(uuid.uuid4())
    prospect_id = str(uuid.uuid4())

    cur.execute("""
        INSERT INTO asx_listings
            (id, ticker, company_name, gics_industry_group, gics_sector,
             is_target_sector, market_cap_aud, is_active)
        VALUES (%s, %s, %s, 'Materials', 'Materials', TRUE, %s, TRUE)
    """, (listing_id, ticker, f"Test Co {ticker}", market_cap_cents))

    cur.execute("""
        INSERT INTO prospect_matrix (id, listing_id, status, status_changed_by)
        VALUES (%s, %s, %s, 'test')
    """, (prospect_id, listing_id, status))

    return prospect_id


# ---------------------------------------------------------------------------
# BASELINE tests — run these BEFORE modifying asx_scraper.py
# ---------------------------------------------------------------------------

def test_baseline_gate1_function_absent():
    """apply_gate1_dq should NOT exist in asx_scraper before the modification."""
    import importlib
    import asx_scraper
    importlib.reload(asx_scraper)
    assert not hasattr(asx_scraper, "apply_gate1_dq"), (
        "apply_gate1_dq already exists — scraper has already been modified. "
        "This baseline test is no longer valid."
    )


def test_baseline_new_prospect_status(db_conn):
    """Before Gate 1 exists, a sub-$5M prospect is inserted as 'unscreened', not DQ'd."""
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        prospect_id = _insert_test_prospect(cur, "ZZT1", CAP_3M, status="unscreened")
        cur.execute("SELECT status, dq_reason FROM prospect_matrix WHERE id = %s",
                    (prospect_id,))
        row = cur.fetchone()
    assert row["status"] == "unscreened", f"Expected 'unscreened', got '{row['status']}'"
    assert row["dq_reason"] is None, "Expected no dq_reason before Gate 1 exists"


# ---------------------------------------------------------------------------
# MODIFIED tests — run these AFTER modifying asx_scraper.py
# ---------------------------------------------------------------------------

def test_gate1_function_present():
    """apply_gate1_dq must exist in asx_scraper after modification."""
    import importlib
    import asx_scraper
    importlib.reload(asx_scraper)
    assert hasattr(asx_scraper, "apply_gate1_dq"), (
        "apply_gate1_dq not found — scraper modification did not add the function."
    )


def test_gate1_sub5m_triggers_dq(db_conn):
    """market_cap $3M AUD (300M cents) → should be DQ'd by Gate 1."""
    import importlib
    import asx_scraper
    importlib.reload(asx_scraper)

    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        prospect_id = _insert_test_prospect(cur, "ZZT1", CAP_3M)

    n = asx_scraper.apply_gate1_dq(db_conn)

    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT status, dq_reason FROM prospect_matrix WHERE id = %s",
                    (prospect_id,))
        row = cur.fetchone()

    assert n >= 1, f"Expected at least 1 DQ, got {n}"
    assert row["status"] == "disqualified", f"Expected 'disqualified', got '{row['status']}'"
    assert row["dq_reason"] is not None and "Gate 1" in row["dq_reason"]


def test_gate1_zero_cap_skipped(db_conn):
    """market_cap $0 (stale/unknown) → should NOT be DQ'd."""
    import importlib
    import asx_scraper
    importlib.reload(asx_scraper)

    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        prospect_id = _insert_test_prospect(cur, "ZZT2", CAP_0)

    asx_scraper.apply_gate1_dq(db_conn)

    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT status FROM prospect_matrix WHERE id = %s", (prospect_id,))
        row = cur.fetchone()

    assert row["status"] == "unscreened", (
        f"Zero-cap row should stay 'unscreened', got '{row['status']}'"
    )


def test_gate1_30m_cap_skipped(db_conn):
    """market_cap $30M AUD (3B cents) → should NOT be DQ'd."""
    import importlib
    import asx_scraper
    importlib.reload(asx_scraper)

    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        prospect_id = _insert_test_prospect(cur, "ZZT3", CAP_30M)

    asx_scraper.apply_gate1_dq(db_conn)

    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT status FROM prospect_matrix WHERE id = %s", (prospect_id,))
        row = cur.fetchone()

    assert row["status"] == "unscreened", (
        f"$30M cap row should stay 'unscreened', got '{row['status']}'"
    )


def test_gate1_100m_cap_skipped(db_conn):
    """market_cap $100M AUD (10B cents) → should NOT be DQ'd."""
    import importlib
    import asx_scraper
    importlib.reload(asx_scraper)

    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        prospect_id = _insert_test_prospect(cur, "ZZT4", CAP_100M)

    asx_scraper.apply_gate1_dq(db_conn)

    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT status FROM prospect_matrix WHERE id = %s", (prospect_id,))
        row = cur.fetchone()

    assert row["status"] == "unscreened", (
        f"$100M cap row should stay 'unscreened', got '{row['status']}'"
    )


def test_gate1_already_dq_untouched(db_conn):
    """Already disqualified row → Gate 1 must NOT overwrite dq_reason."""
    import importlib
    import asx_scraper
    importlib.reload(asx_scraper)

    original_reason = "Manually DQ'd for other reasons"
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        prospect_id = _insert_test_prospect(cur, "ZZT5", CAP_3M, status="disqualified")
        cur.execute(
            "UPDATE prospect_matrix SET dq_reason = %s WHERE id = %s",
            (original_reason, prospect_id)
        )

    asx_scraper.apply_gate1_dq(db_conn)

    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT status, dq_reason FROM prospect_matrix WHERE id = %s",
                    (prospect_id,))
        row = cur.fetchone()

    assert row["status"] == "disqualified"
    assert row["dq_reason"] == original_reason, (
        f"dq_reason was overwritten: '{row['dq_reason']}'"
    )
