"""Phase 1: snapshot seeding never overwrites analyst edits; evidence date preserved."""

import json

from psycopg2.extras import RealDictCursor

import api
from tests.conftest import requires_db

pytestmark = requires_db


def _write_snapshot(dir_, ticker, as_of="2026-03-31", revenue="$10M"):
    (dir_ / f"{ticker.lower()}_snapshot.json").write_text(json.dumps({
        "snapshot_data": {"ticker": ticker, "as_of": as_of, "financials": {"revenue": revenue}},
        "snapshot_markdown": f"# {ticker}\nRevenue {revenue}",
    }), encoding="utf-8")


def _snapshot(conn, prospect_id):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM pitchbook_snapshots WHERE prospect_id = %s", (prospect_id,))
        return cur.fetchone()


def test_seed_inserts_missing_with_evidence_date(db_conn, factory, tmp_path):
    p = factory.create()
    _write_snapshot(tmp_path, p["ticker"])
    stats = api.seed_snapshots(db_conn, tmp_path, mode="insert_missing")
    assert stats["inserted"] == 1
    row = _snapshot(db_conn, p["prospect_id"])
    assert row["enriched_by"] == "seed"
    assert str(row["source_as_of"]) == "2026-03-31", "as_of from the file is the evidence date"
    assert row["imported_at"] is not None and row["source_file"].endswith("_snapshot.json")
    assert row["enriched_at"].date().isoformat() == "2026-03-31", "display date reflects evidence, not import"


def test_seed_does_not_touch_existing_rows_by_default(db_conn, factory, tmp_path):
    p = factory.create()
    _write_snapshot(tmp_path, p["ticker"], revenue="$10M")
    api.seed_snapshots(db_conn, tmp_path)
    _write_snapshot(tmp_path, p["ticker"], revenue="$99M")
    stats = api.seed_snapshots(db_conn, tmp_path)
    assert stats["skipped_existing"] == 1 and stats["updated"] == 0
    assert _snapshot(db_conn, p["prospect_id"])["snapshot_data"]["financials"]["revenue"] == "$10M"


def test_overwrite_mode_refreshes_seeded_rows_but_never_manual_edits(db_conn, factory, tmp_path):
    seeded = factory.create()
    edited = factory.create()
    _write_snapshot(tmp_path, seeded["ticker"], revenue="$1M")
    _write_snapshot(tmp_path, edited["ticker"], revenue="$1M")
    api.seed_snapshots(db_conn, tmp_path)
    # analyst edits one of them
    with db_conn.cursor() as cur:
        cur.execute("""UPDATE pitchbook_snapshots SET snapshot_markdown = 'analyst notes', enriched_by = 'manual',
                       edited_at = NOW() WHERE prospect_id = %s""", (edited["prospect_id"],))
    db_conn.commit()
    _write_snapshot(tmp_path, seeded["ticker"], revenue="$2M")
    _write_snapshot(tmp_path, edited["ticker"], revenue="$2M")
    stats = api.seed_snapshots(db_conn, tmp_path, mode="overwrite")
    assert stats["updated"] == 1 and stats["skipped_manual"] == 1
    assert _snapshot(db_conn, seeded["prospect_id"])["snapshot_data"]["financials"]["revenue"] == "$2M"
    assert _snapshot(db_conn, edited["prospect_id"])["snapshot_markdown"] == "analyst notes"


def test_manual_api_upsert_marks_row_as_manual(db_conn, factory):
    from fastapi.testclient import TestClient
    p = factory.create()
    with TestClient(api.app) as client:
        r = client.post(f"/api/prospects/{p['prospect_id']}/snapshot",
                        json={"snapshot_data": {"as_of": "2026-06-30"}, "snapshot_markdown": "hello"})
        assert r.status_code == 200, r.text
    row = _snapshot(db_conn, p["prospect_id"])
    assert row["enriched_by"] == "manual" and row["edited_at"] is not None
    assert str(row["source_as_of"]) == "2026-06-30"
