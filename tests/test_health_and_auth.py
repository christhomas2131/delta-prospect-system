"""Phase 1: readiness vs liveness, sanitised 503, auth gating, request ids, log scrubbing."""

import base64
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import api
import observability


@pytest.fixture
def client(monkeypatch):
    """App with runtime init stubbed and a controllable fake pool."""
    monkeypatch.setattr(api, "init_runtime", lambda: None)
    pool = MagicMock()
    monkeypatch.setattr(api, "db_pool", pool)
    with TestClient(api.app) as c:
        yield c, pool


def _good_conn():
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (7,)
    cur.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def test_live_never_touches_db(client):
    c, pool = client
    pool.getconn.side_effect = AssertionError("must not use db")
    r = c.get("/api/health/live")
    assert r.status_code == 200 and r.json()["status"] == "alive"
    assert r.headers["x-request-id"]


def test_ready_returns_503_without_leaking_db_error(client, monkeypatch):
    c, pool = client
    pool.getconn.side_effect = RuntimeError("FATAL: password authentication failed for user delta at 10.0.0.5")
    r = c.get("/api/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unhealthy" and body["detail"] == "database unavailable"
    assert "password" not in r.text and "10.0.0.5" not in r.text


def test_ready_reports_auth_and_migrations(client, monkeypatch):
    c, pool = client
    pool.getconn.side_effect = None
    pool.getconn.return_value = _good_conn()
    monkeypatch.setattr(api.migrations, "pending", lambda conn: [])
    r = c.get("/api/health")
    assert r.status_code == 200
    assert r.json()["auth_configured"] is False
    assert r.json()["migrations_pending"] == 0


def test_auth_required_when_configured(client, monkeypatch):
    c, pool = client
    monkeypatch.setattr(api, "AUTH_CONFIGURED", True)
    monkeypatch.setattr(api, "AUTH_USER", "op")
    monkeypatch.setattr(api, "AUTH_PASSWORD", "secret")
    assert c.get("/api/health/live").status_code == 200, "health stays public"
    r = c.get("/api/jobs")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    bad = base64.b64encode(b"op:wrong").decode()
    assert c.get("/api/jobs", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    pool.getconn.return_value = _good_conn()
    good = base64.b64encode(b"op:secret").decode()
    monkeypatch.setattr(api.jobs, "list_jobs", lambda *a, **k: [])
    monkeypatch.setattr(api.jobs, "queue_stats", lambda conn: {})
    assert c.get("/api/jobs", headers={"Authorization": f"Basic {good}"}).status_code == 200


def test_partial_auth_config_is_flagged(monkeypatch, caplog):
    monkeypatch.setattr(api, "AUTH_CONFIGURED", False)
    monkeypatch.setattr(api, "AUTH_USER", "op")
    monkeypatch.setattr(api, "AUTH_PASSWORD", None)
    with caplog.at_level("ERROR"):
        api._check_auth_config()
    assert any("MISCONFIGURED" in m for m in caplog.messages)


def test_production_without_auth_warns(monkeypatch, caplog):
    monkeypatch.setattr(api, "AUTH_CONFIGURED", False)
    monkeypatch.setattr(api, "AUTH_USER", None)
    monkeypatch.setattr(api, "AUTH_PASSWORD", None)
    monkeypatch.setattr(api, "APP_ENV", "production")
    with caplog.at_level("WARNING"):
        api._check_auth_config()
    assert any("NO authentication" in m for m in caplog.messages)


def test_log_scrubber_hides_secrets():
    text = "key sk-ant-api03-abcdefghijklmnop url postgresql://delta:delta_dev@host/db api_key=ABCD1234 ok"
    out = observability.scrub(text)
    assert "sk-ant-api03-abcdefghijklmnop" not in out and "sk-ant-***" in out
    assert "delta_dev" not in out and "delta:***@" in out
    assert "ABCD1234" not in out
