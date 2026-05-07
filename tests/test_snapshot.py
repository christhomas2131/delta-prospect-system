"""
Roundtrip test: POST /api/prospects/{id}/snapshot → GET /api/prospects/{id}/snapshot

Uses unittest.mock to patch the DB pool so no real PostgreSQL connection is needed.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import uuid
import pytest
from unittest.mock import MagicMock, patch

import api
from fastapi.testclient import TestClient

PROSPECT_ID = str(uuid.uuid4())
SNAPSHOT_ID = str(uuid.uuid4())
SAMPLE_DATA = {"revenue": "$120M", "ebitda_margin": "18%", "employees": 850}
SAMPLE_MD   = "# Acme Resources\n\n**Revenue:** $120M\n\n- EBITDA margin: 18%\n- Employees: 850"

SNAPSHOT_ROW = {
    "id": SNAPSHOT_ID,
    "prospect_id": PROSPECT_ID,
    "snapshot_data": SAMPLE_DATA,
    "snapshot_markdown": SAMPLE_MD,
    "enriched_at": "2026-05-07T10:00:00",
    "enriched_by": "manual",
}


def _mock_conn(fetchone=None):
    """Build a mock psycopg2 connection whose cursor.fetchone() returns `fetchone`."""
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = fetchone
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


@pytest.fixture(scope="module")
def app_client():
    """Start the FastAPI test client with the DB pool fully mocked."""
    pool = MagicMock()
    pool.getconn.return_value = _mock_conn()
    pool.putconn = MagicMock()

    with patch("psycopg2.pool.ThreadedConnectionPool", return_value=pool):
        with TestClient(api.app) as client:
            yield client, pool


def test_post_snapshot(app_client):
    client, pool = app_client
    pool.getconn.return_value = _mock_conn(fetchone=SNAPSHOT_ROW)

    resp = client.post(
        f"/api/prospects/{PROSPECT_ID}/snapshot",
        json={"snapshot_data": SAMPLE_DATA, "snapshot_markdown": SAMPLE_MD},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["prospect_id"] == PROSPECT_ID
    assert body["snapshot_markdown"] == SAMPLE_MD
    assert body["snapshot_data"] == SAMPLE_DATA


def test_get_snapshot(app_client):
    client, pool = app_client
    pool.getconn.return_value = _mock_conn(fetchone=SNAPSHOT_ROW)

    resp = client.get(f"/api/prospects/{PROSPECT_ID}/snapshot")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["prospect_id"] == PROSPECT_ID
    assert body["snapshot_data"] == SAMPLE_DATA


def test_get_snapshot_not_found(app_client):
    client, pool = app_client
    pool.getconn.return_value = _mock_conn(fetchone=None)

    resp = client.get(f"/api/prospects/{uuid.uuid4()}/snapshot")
    assert resp.status_code == 404
