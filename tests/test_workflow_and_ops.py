"""Phase 2/4/5 through the HTTP API: concurrency, activity, signal review, review queue,
DQ reinstate, ops health, metrics, score policy endpoint. Runs against the dev DB."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import RealDictCursor

import api
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture(scope="module")
def client():
    with TestClient(api.app) as c:
        yield c


def test_patch_logs_activity_and_detects_concurrent_edit(client, factory, db_conn):
    p = factory.create()
    r = client.patch(f"/api/prospects/{p['prospect_id']}", json={"owner": "sam", "opportunity_stage": "researching", "actor": "sam"})
    assert r.status_code == 200, r.text
    v1 = r.json()["row_version"]
    # someone else edits
    r2 = client.patch(f"/api/prospects/{p['prospect_id']}", json={"next_action": "call COO", "next_action_due": str(date.today() - timedelta(days=1))})
    assert r2.status_code == 200 and r2.json()["row_version"] > v1
    # stale save is rejected, nothing written
    r3 = client.patch(f"/api/prospects/{p['prospect_id']}", json={"owner": "alex", "expected_version": v1})
    assert r3.status_code == 409 and r3.json()["detail"]["current_version"] == r2.json()["row_version"]
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT owner, stage_changed_at FROM prospect_matrix WHERE id = %s", (p["prospect_id"],))
        row = cur.fetchone()
    assert row["owner"] == "sam" and row["stage_changed_at"] is not None
    acts = client.get(f"/api/prospects/{p['prospect_id']}/activity").json()["activity"]
    kinds = {(a["activity_type"], a["field"]) for a in acts}
    assert ("field_change", "owner") in kinds and ("stage_change", "opportunity_stage") in kinds
    assert all(a["actor"] in ("sam", "api") for a in acts)
    # bad inputs
    assert client.patch(f"/api/prospects/{p['prospect_id']}", json={"opportunity_stage": "sideways"}).status_code == 400
    assert client.patch(f"/api/prospects/{p['prospect_id']}", json={"next_action_due": "soon"}).status_code == 400
    # clearing with ""
    r4 = client.patch(f"/api/prospects/{p['prospect_id']}", json={"next_action_due": ""})
    assert r4.status_code == 200
    with db_conn.cursor() as cur:
        cur.execute("SELECT next_action_due FROM prospect_matrix WHERE id = %s", (p["prospect_id"],))
        assert cur.fetchone()[0] is None


def test_signal_review_rescores_and_retiers(client, factory, db_conn):
    p = factory.create()
    sid = factory.add_signal(p["prospect_id"], pillar="production", strength="strong")
    client.post(f"/api/prospects/{p['prospect_id']}/rescore")
    r = client.patch(f"/api/signals/{sid}", json={"is_valid": False, "note": "routine filing", "actor": "sam"})
    assert r.status_code == 200
    assert float(r.json()["prospect_score"]) == 0 and r.json()["lead_tier"] == "not_qualified"
    detail = client.get(f"/api/prospects/{p['prospect_id']}").json()
    sig = detail["signals"][0]
    assert sig["verification_status"] == "analyst_rejected" and sig["analyst_note"] == "routine filing"
    assert "evidence_age_days" in sig
    assert any(a["activity_type"] == "signal_review" for a in detail["activity"])
    r = client.patch(f"/api/signals/{sid}", json={"is_valid": True})
    assert float(r.json()["prospect_score"]) > 0 and r.json()["lead_tier"] == "warm"
    assert client.patch("/api/signals/00000000-0000-0000-0000-000000000000", json={"is_valid": True}).status_code == 404


def test_review_queue_reasons(client, factory, db_conn):
    overdue = factory.create(owner="sam", next_action="ping", next_action_due=date.today() - timedelta(days=2))
    fresh = factory.create(is_watchlisted=True)
    factory.add_signal(fresh["prospect_id"])
    stale = factory.create(is_watchlisted=True)
    data = client.get("/api/review-queue?limit=500").json()
    by_id = {}
    for it in data["items"]:
        by_id.setdefault(it["prospect_id"], set()).add(it["reason"])
    assert "overdue_action" in by_id[overdue["prospect_id"]]
    assert "new_evidence" in by_id[fresh["prospect_id"]]
    assert "stale_research" in by_id[stale["prospect_id"]], "watchlisted with no enrichment ever -> stale"
    assert data["counts"]["overdue_action"] >= 1
    owned = client.get("/api/review-queue?owner=sam").json()
    assert all(i["owner"] == "sam" or i["reason"] == "dq_review" for i in owned["items"])


def test_dq_review_queue_and_reinstate(client, factory, db_conn):
    # DQ'd under gate 1 at $3M, now worth $20M
    p = factory.create(market_cap_cents=2_000_000_000, status="disqualified", dq_reason="Phase 0 Gate 1 ...",
                       dq_reason_code="gate1_market_cap", dq_observed_cap_cents=300_000_000)
    still = factory.create(market_cap_cents=300_000_000, status="disqualified", dq_reason="Phase 0 Gate 1 ...",
                           dq_reason_code="gate1_market_cap", dq_observed_cap_cents=300_000_000)
    q = client.get("/api/dq/review-queue").json()
    ids = {i["prospect_id"] for i in q["items"]}
    assert p["prospect_id"] in ids and still["prospect_id"] not in ids
    r = client.post(f"/api/prospects/{p['prospect_id']}/dq/reinstate", json={"note": "recovered", "actor": "sam"})
    assert r.status_code == 200 and r.json()["status"] == "qualified"
    assert client.post(f"/api/prospects/{p['prospect_id']}/dq/reinstate", json={}).status_code == 409
    acts = client.get(f"/api/prospects/{p['prospect_id']}/activity").json()["activity"]
    assert any(a["activity_type"] == "dq_reinstated" and a["payload"]["previous_dq_reason_code"] == "gate1_market_cap" for a in acts)
    # dismiss keeps DQ and removes from queue
    r = client.post(f"/api/prospects/{still['prospect_id']}/dq/dismiss-review", json={})
    assert r.status_code == 200
    with db_conn.cursor() as cur:
        cur.execute("UPDATE asx_listings SET market_cap_aud = 2000000000 WHERE id = %s", (still["listing_id"],))
    db_conn.commit()
    assert still["prospect_id"] not in {i["prospect_id"] for i in client.get("/api/dq/review-queue").json()["items"]}


def test_manual_dq_via_patch_records_reason_code(client, factory, db_conn):
    p = factory.create()
    r = client.patch(f"/api/prospects/{p['prospect_id']}", json={"status": "disqualified", "dq_reason": "not a fit"})
    assert r.status_code == 200
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT dq_reason_code, dq_applied_at FROM prospect_matrix WHERE id = %s", (p["prospect_id"],))
        row = cur.fetchone()
    assert row["dq_reason_code"] == "manual" and row["dq_applied_at"] is not None


def test_report_contains_provenance_workflow_and_labels(client, factory):
    p = factory.create(owner="sam", next_action="call", opportunity_stage="contacted")
    factory.add_signal(p["prospect_id"], verification_status="analyst_confirmed")
    client.post(f"/api/prospects/{p['prospect_id']}/rescore")
    r = client.get(f"/api/prospects/{p['prospect_id']}/report?format=csv")
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    for needle in ("Research provenance", "Score policy", "Workflow", "Owner,sam", "Next action,call", "Verification,analyst_confirmed"):
        assert needle in text, needle
    acts = client.get(f"/api/prospects/{p['prospect_id']}/activity").json()["activity"]
    assert any(a["activity_type"] == "report_export" for a in acts)
    docx = client.get(f"/api/prospects/{p['prospect_id']}/report?format=docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"


def test_ops_health_metrics_and_events(client):
    h = client.get("/api/ops/health").json()
    for key in ("freshness", "jobs", "ai_spend", "recent_events", "targets", "auth_configured"):
        assert key in h
    assert h["freshness"]["target_companies"] > 0
    assert client.get("/api/ops/events").status_code == 200
    f = client.get("/api/metrics/funnel").json()
    assert "by_stage" in f and "caveat" in f and "cohorts" in f
    c = client.get("/api/metrics/costs").json()
    assert "months" in c and "current" in c
    fired = client.post("/api/ops/alerts/check").json()["fired"]
    assert isinstance(fired, list)
    est = client.get("/api/ai/estimate?companies=10").json()
    assert est["companies"] == 10 and "est_total_usd" in est and est["api_key_configured"] is False
    assert client.get("/api/ai/ledger").status_code == 200


def test_score_policy_endpoint_round_trip(client, factory):
    p = factory.create()
    factory.add_signal(p["prospect_id"], source_date=date.today() - timedelta(days=600))
    before = client.get("/api/settings/score-policy").json()
    assert before["version"] == "v1" and "policies" in before
    try:
        r = client.put("/api/settings/score-policy", json={"version": "v2", "rescore": False})
        assert r.status_code == 200 and r.json()["version"] == "v2"
        detail = client.post(f"/api/prospects/{p['prospect_id']}/rescore").json()
        assert detail["score_policy_version"] == "v2" and float(detail["prospect_score"]) == float(detail["score_v2"])
    finally:
        client.put("/api/settings/score-policy", json={"version": "v1", "rescore": False})
        client.post(f"/api/prospects/{p['prospect_id']}/rescore")
    assert client.put("/api/settings/score-policy", json={"version": "v9"}).status_code == 422


def test_contact_rows_carry_provenance_labels(client, factory, db_conn):
    p = factory.create()
    with db_conn.cursor() as cur:
        cur.execute("""INSERT INTO prospect_contacts (prospect_id, full_name, normalized_name, title, email, email_status,
                       source_provider, source_contact_id, confidence_score)
                       VALUES (%s, 'Investor Relations', 'investor relations', 'Investor Relations', 'investors@x.com', 'unverified', 'public_website', 'investors@x.com', 0.5)""",
                    (p["prospect_id"],))
    db_conn.commit()
    rows = client.get(f"/api/prospects/{p['prospect_id']}/contacts").json()["contacts"]
    assert rows[0]["contact_kind"] == "generic_inbox" and "raw_data" not in rows[0]


def test_cron_endpoint_enqueues_instead_of_blocking(client, monkeypatch, db_conn):
    import jobs
    monkeypatch.setattr(api, "CRON_SECRET", "s3cret")
    assert client.post("/api/cron/enrich-all?token=wrong").status_code == 401
    r = client.post("/api/cron/enrich-all?token=s3cret")
    assert r.status_code == 200 and r.json()["status"] == "queued"
    for jid in (r.json()["refresh_job_id"], r.json()["enrich_job_id"]):
        jobs.cancel(db_conn, jid)
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM background_jobs WHERE id = ANY(%s::uuid[])", ([r.json()["refresh_job_id"], r.json()["enrich_job_id"]],))
    db_conn.commit()
