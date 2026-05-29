"""
Phase 0 Gate 2 smoke tests — integration tests against the real dev DB.

Gate 2 rule: day_volume = 0 AND market_cap_aud > 0 AND market_cap_aud < AUD $50M
             → status = 'disqualified'

Rows with day_volume IS NULL are excluded (volume not yet fetched).
Rows with day_volume > 0 are excluded (active trader).
Rows with market_cap >= AUD $50M are excluded (above threshold).
Already disqualified/archived rows are never touched.

BASELINE (before modification):
  test_baseline_gate2_function_absent — apply_gate2_dq does not exist yet.

MODIFIED (after modification):
  test_gate2_zero_volume_sub50m_triggers_dq — volume=0, cap=$30M → disqualified
  test_gate2_null_volume_skipped            — volume=NULL → NOT DQ'd
  test_gate2_nonzero_volume_skipped         — volume=5000 → NOT DQ'd
  test_gate2_zero_volume_over50m_skipped    — volume=0, cap=$100M → NOT DQ'd
  test_gate2_already_dq_untouched           — already disqualified → reason unchanged
"""

import os, sys, uuid, pytest, psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 5432)),
    "dbname":   os.getenv("DB_NAME", "delta_prospect"),
    "user":     os.getenv("DB_USER", "delta"),
    "password": os.getenv("DB_PASSWORD", "delta_dev"),
}

TEST_TICKERS = ["ZZG1", "ZZG2", "ZZG3", "ZZG4", "ZZG5"]

# market_cap_aud in cents
CAP_30M  = 3_000_000_000   # $30M — should trigger Gate 2 if volume=0
CAP_100M =10_000_000_000   # $100M — above $50M threshold, should NOT trigger


@pytest.fixture
def db_conn():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    yield conn
    with conn.cursor() as cur:
        cur.execute("DELETE FROM asx_listings WHERE ticker = ANY(%s)", (TEST_TICKERS,))
    conn.commit()
    conn.close()


def _insert_test_prospect(cur, ticker, market_cap_cents, day_volume, status="unscreened"):
    listing_id = str(uuid.uuid4())
    prospect_id = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO asx_listings
            (id, ticker, company_name, gics_industry_group, gics_sector,
             is_target_sector, market_cap_aud, day_volume, is_active)
        VALUES (%s, %s, %s, 'Materials', 'Materials', TRUE, %s, %s, TRUE)
    """, (listing_id, ticker, f"Test Gate2 Co {ticker}", market_cap_cents, day_volume))
    cur.execute("""
        INSERT INTO prospect_matrix (id, listing_id, status, status_changed_by)
        VALUES (%s, %s, %s::prospect_status, 'test')
    """, (prospect_id, listing_id, status))
    return prospect_id


# ---------------------------------------------------------------------------
# BASELINE
# ---------------------------------------------------------------------------

def test_baseline_gate2_function_absent():
    """apply_gate2_dq should NOT exist before modification."""
    import importlib, asx_scraper
    importlib.reload(asx_scraper)
    assert not hasattr(asx_scraper, "apply_gate2_dq"), (
        "apply_gate2_dq already exists — scraper has already been modified."
    )


# ---------------------------------------------------------------------------
# MODIFIED
# ---------------------------------------------------------------------------

def test_gate2_function_present():
    import importlib, asx_scraper
    importlib.reload(asx_scraper)
    assert hasattr(asx_scraper, "apply_gate2_dq")


def test_gate2_zero_volume_sub50m_triggers_dq(db_conn):
    """volume=0 + cap=$30M → should be DQ'd."""
    import importlib, asx_scraper
    importlib.reload(asx_scraper)
    with db_conn.cursor() as cur:
        pid = _insert_test_prospect(cur, "ZZG1", CAP_30M, day_volume=0)
    n = asx_scraper.apply_gate2_dq(db_conn)
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT status, dq_reason FROM prospect_matrix WHERE id=%s", (pid,))
        row = cur.fetchone()
    assert n >= 1
    assert row["status"] == "disqualified"
    assert row["dq_reason"] is not None and "Gate 2" in row["dq_reason"]


def test_gate2_null_volume_skipped(db_conn):
    """volume=NULL (not yet fetched) → should NOT be DQ'd."""
    import importlib, asx_scraper
    importlib.reload(asx_scraper)
    with db_conn.cursor() as cur:
        pid = _insert_test_prospect(cur, "ZZG2", CAP_30M, day_volume=None)
    asx_scraper.apply_gate2_dq(db_conn)
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT status FROM prospect_matrix WHERE id=%s", (pid,))
        row = cur.fetchone()
    assert row["status"] == "unscreened", f"NULL volume row should stay 'unscreened', got '{row['status']}'"


def test_gate2_nonzero_volume_skipped(db_conn):
    """volume=5000 (active trader) → should NOT be DQ'd."""
    import importlib, asx_scraper
    importlib.reload(asx_scraper)
    with db_conn.cursor() as cur:
        pid = _insert_test_prospect(cur, "ZZG3", CAP_30M, day_volume=5000)
    asx_scraper.apply_gate2_dq(db_conn)
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT status FROM prospect_matrix WHERE id=%s", (pid,))
        row = cur.fetchone()
    assert row["status"] == "unscreened", f"Active-volume row should stay 'unscreened', got '{row['status']}'"


def test_gate2_zero_volume_over50m_skipped(db_conn):
    """volume=0 but cap=$100M (above $50M threshold) → should NOT be DQ'd."""
    import importlib, asx_scraper
    importlib.reload(asx_scraper)
    with db_conn.cursor() as cur:
        pid = _insert_test_prospect(cur, "ZZG4", CAP_100M, day_volume=0)
    asx_scraper.apply_gate2_dq(db_conn)
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT status FROM prospect_matrix WHERE id=%s", (pid,))
        row = cur.fetchone()
    assert row["status"] == "unscreened", f"Over-$50M row should stay 'unscreened', got '{row['status']}'"


def test_gate2_already_dq_untouched(db_conn):
    """Already disqualified row → Gate 2 must NOT overwrite dq_reason."""
    import importlib, asx_scraper
    importlib.reload(asx_scraper)
    original_reason = "Manually DQ'd before Gate 2"
    with db_conn.cursor() as cur:
        pid = _insert_test_prospect(cur, "ZZG5", CAP_30M, day_volume=0, status="disqualified")
        cur.execute("UPDATE prospect_matrix SET dq_reason=%s WHERE id=%s", (original_reason, pid))
    asx_scraper.apply_gate2_dq(db_conn)
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT status, dq_reason FROM prospect_matrix WHERE id=%s", (pid,))
        row = cur.fetchone()
    assert row["dq_reason"] == original_reason, f"dq_reason was overwritten: '{row['dq_reason']}'"
