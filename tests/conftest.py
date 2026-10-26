"""
Shared fixtures.

Integration tests run against the dev PostgreSQL (delta/delta_dev/delta_prospect
by default, overridable with DB_* env vars). Every fixture that writes data uses
tickers prefixed ``ZZ`` and deletes them at teardown, so the real prospect data
is never touched. Tests that need the database are skipped when it is not
reachable, so the pure-unit tests still run anywhere.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Never let tests spend money or spawn background work.
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["FIRECRAWL_API_KEY"] = ""
os.environ["RUN_WORKER"] = "false"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["AI_MONTHLY_BUDGET_USD"] = os.environ.get("AI_MONTHLY_BUDGET_USD", "0")

import psycopg2  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402

from db import DB_CONFIG  # noqa: E402


def _db_available() -> bool:
    try:
        conn = psycopg2.connect(connect_timeout=3, **DB_CONFIG)
        conn.close()
        return True
    except Exception:
        return False


DB_AVAILABLE = _db_available()
requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason="PostgreSQL dev database not reachable")


@pytest.fixture
def db_conn():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    yield conn
    try:
        conn.rollback()
    finally:
        conn.close()


class ProspectFactory:
    """Creates throwaway listings/prospects (tickers ZZxx) and removes them afterwards."""

    def __init__(self, conn):
        self.conn = conn
        self.tickers: list[str] = []

    def create(self, *, ticker: str | None = None, market_cap_cents: int = 20_000_000_000, day_volume: int | None = 1000,
               status: str = "enriched", sector: str = "Materials", likelihood: int = 5, **matrix_fields) -> dict:
        ticker = ticker or f"ZZ{uuid.uuid4().hex[:4].upper()}"
        self.tickers.append(ticker)
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO asx_listings (ticker, company_name, gics_industry_group, gics_sector, is_target_sector,
                                             market_cap_aud, day_volume, is_active, website)
                   VALUES (%s, %s, %s, %s, TRUE, %s, %s, TRUE, 'https://example.com')
                   ON CONFLICT (ticker) DO UPDATE SET market_cap_aud = EXCLUDED.market_cap_aud,
                                                      day_volume = EXCLUDED.day_volume
                   RETURNING id""",
                (ticker, f"{ticker} Test Mining Ltd", sector, sector, market_cap_cents, day_volume),
            )
            listing_id = cur.fetchone()["id"]
            cur.execute("DELETE FROM prospect_matrix WHERE listing_id = %s", (listing_id,))
            cols = ["listing_id", "status", "likelihood_score"] + list(matrix_fields)
            vals = [listing_id, status, likelihood] + list(matrix_fields.values())
            placeholders = ", ".join(["%s", "%s::prospect_status", "%s"] + ["%s"] * len(matrix_fields))
            cur.execute(
                f"INSERT INTO prospect_matrix ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
                vals,
            )
            prospect_id = cur.fetchone()["id"]
        self.conn.commit()
        return {"ticker": ticker, "listing_id": str(listing_id), "prospect_id": str(prospect_id)}

    def add_signal(self, prospect_id: str, *, pillar: str = "production", strength: str = "strong",
                   summary: str = "Production guidance cut", source_url: str | None = None,
                   source_date=None, confidence: float = 0.85, is_valid=None, **extra) -> str:
        source_url = source_url or f"https://example.com/ann/{uuid.uuid4().hex[:8]}"
        cols = ["prospect_id", "pressure_type", "strength", "summary", "source_type", "source_url",
                "source_title", "source_date", "confidence_score", "is_valid"] + list(extra)
        vals = [prospect_id, pillar, strength, summary, "asx_announcement", source_url,
                summary, source_date, confidence, is_valid] + list(extra.values())
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO pressure_signals ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                vals,
            )
            sid = str(cur.fetchone()[0])
        self.conn.commit()
        return sid

    def cleanup(self):
        if not self.tickers:
            return
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM asx_listings WHERE ticker = ANY(%s)", (self.tickers,))
        self.conn.commit()


@pytest.fixture
def factory(db_conn):
    f = ProspectFactory(db_conn)
    yield f
    f.cleanup()


@pytest.fixture
def job_cleanup(db_conn):
    """Delete test jobs (job_type starting with 'test_') after the test."""
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM background_jobs WHERE job_type LIKE 'test\\_%'")
    db_conn.commit()
