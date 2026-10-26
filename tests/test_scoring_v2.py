"""Phase 3: explainable scoring, recency decay, per-event damping, tier/score alignment, policy switch."""

from datetime import date, timedelta

from psycopg2.extras import RealDictCursor

from tests.conftest import requires_db

pytestmark = requires_db


def _score(conn, pid):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT calculate_prospect_score(%s) AS s, calculate_lead_tier(%s) AS t", (pid, pid))
        row = cur.fetchone()
        cur.execute("SELECT prospect_score, score_v1, score_v2, score_breakdown, lead_tier, score_policy_version FROM prospect_matrix WHERE id = %s", (pid,))
        pm = cur.fetchone()
    conn.commit()
    return float(row["s"]), row["t"], dict(pm)


def test_breakdown_is_present_and_v1_remains_default(db_conn, factory):
    p = factory.create(likelihood=5)
    factory.add_signal(p["prospect_id"], pillar="production", strength="strong", source_date=date.today())
    score, tier, pm = _score(db_conn, p["prospect_id"])
    assert pm["score_policy_version"] == "v1"
    assert score == float(pm["score_v1"]) == 6.0  # 3*4*5/10
    b = pm["score_breakdown"]
    assert b["policy"] == "v2" and b["displayed_policy"] == "v1"
    assert b["pillars"]["production"]["signals"] == 1
    assert b["days_since_latest_evidence"] == 0 and b["signals_used"] == 1
    assert float(pm["score_v2"]) > 0


def test_recency_decay_reduces_old_evidence_in_v2_but_not_v1(db_conn, factory):
    fresh = factory.create(); old = factory.create()
    factory.add_signal(fresh["prospect_id"], pillar="cost", strength="strong", source_date=date.today() - timedelta(days=10))
    factory.add_signal(old["prospect_id"], pillar="cost", strength="strong", source_date=date.today() - timedelta(days=800))
    _, _, pf = _score(db_conn, fresh["prospect_id"])
    _, _, po = _score(db_conn, old["prospect_id"])
    assert pf["score_v1"] == po["score_v1"], "v1 ignores age"
    assert float(pf["score_v2"]) == 4 * float(po["score_v2"]), "older than 2y counts 0.25x in v2"


def test_repeated_evidence_of_one_event_is_damped_in_v2(db_conn, factory):
    one = factory.create(); five = factory.create()
    factory.add_signal(one["prospect_id"], pillar="production", strength="strong", source_date=date.today())
    for i in range(5):
        factory.add_signal(five["prospect_id"], pillar="production", strength="strong", source_date=date.today(), summary=f"report {i}")
    _, _, p1 = _score(db_conn, one["prospect_id"])
    _, _, p5 = _score(db_conn, five["prospect_id"])
    assert float(p5["score_v1"]) == 5 * float(p1["score_v1"]), "v1 counts five reports as five events"
    ratio = float(p5["score_v2"]) / float(p1["score_v2"])
    assert 2.0 < ratio < 2.5, f"v2 damps repeats geometrically (0.6^k): got {ratio}"


def test_rejected_and_superseded_signals_excluded_from_score_and_tier(db_conn, factory):
    p = factory.create()
    keep = factory.add_signal(p["prospect_id"], pillar="production", strength="strong")
    factory.add_signal(p["prospect_id"], pillar="cost", strength="strong", is_valid=False)
    factory.add_signal(p["prospect_id"], pillar="people", strength="strong", superseded_by=keep)
    score, tier, pm = _score(db_conn, p["prospect_id"])
    assert pm["score_breakdown"]["signals_used"] == 1 and pm["score_breakdown"]["signals_excluded"] == 2
    assert tier == "warm", "one strong pillar -> warm, not hot (excluded rows don't count)"
    assert score == 6.0


def test_score_and_tier_agree_after_enrichment_save(db_conn, factory):
    """save_results must derive tier from persisted signals, not the current batch."""
    from enrichment_agent import save_results
    p = factory.create(status="qualified")
    # Prior run left a strong production signal (persisted).
    factory.add_signal(p["prospect_id"], pillar="production", strength="strong", source_url="https://a/1")
    prospect = {"prospect_id": p["prospect_id"], "listing_id": p["listing_id"], "ticker": p["ticker"]}
    signals = [{"pressure_type": "cost", "strength": "strong", "summary": "Capital raising", "source_url": "https://a/2",
                "source_title": "Capital raising", "source_date": str(date.today()), "confidence": 0.85, "extracted_quote": "x"},
               {"pressure_type": "people", "strength": "strong", "summary": "CEO resigns", "source_url": "https://a/3",
                "source_title": "CEO resigns", "source_date": str(date.today()), "confidence": 0.85, "extracted_quote": "x"}]
    profile = {"strategic_direction": "d", "primary_tailwind": "t", "primary_headwind": "h", "likelihood_score": 5}
    save_results(db_conn, prospect, signals, profile, [], "")
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT lead_tier, status FROM prospect_matrix WHERE id = %s", (p["prospect_id"],))
        row = cur.fetchone()
    assert row["lead_tier"] == "hot", "3 strong pillars across runs -> hot (batch-only logic would say warm)"
    assert row["status"] == "enriched"


def test_policy_switch_changes_displayed_score_only(db_conn, factory):
    p = factory.create()
    factory.add_signal(p["prospect_id"], pillar="production", strength="strong", source_date=date.today() - timedelta(days=500))
    _, _, before = _score(db_conn, p["prospect_id"])
    with db_conn.cursor() as cur:
        cur.execute("UPDATE app_settings SET value = 'v2' WHERE key = 'score_policy_version'")
    db_conn.commit()
    try:
        score, _, after = _score(db_conn, p["prospect_id"])
        assert after["score_policy_version"] == "v2" and score == float(after["score_v2"])
        assert after["score_v1"] == before["score_v1"]
    finally:
        with db_conn.cursor() as cur:
            cur.execute("UPDATE app_settings SET value = 'v1' WHERE key = 'score_policy_version'")
        db_conn.commit()


def test_rescoring_unchanged_evidence_is_stable(db_conn, factory):
    p = factory.create()
    factory.add_signal(p["prospect_id"], pillar="cost", strength="moderate", source_date=date.today())
    a = _score(db_conn, p["prospect_id"])
    b = _score(db_conn, p["prospect_id"])
    assert a[0] == b[0] and a[2]["score_v2"] == b[2]["score_v2"] and a[1] == b[1]
