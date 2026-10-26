"""Phase 2: stable evidence identity, revisions not duplicates, legacy dedup script."""

import sys
from pathlib import Path

from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import api  # noqa: E402
from evidence import ai_source_url, choose_keeper, evidence_key, legacy_duplicate_groups, normalise_claim  # noqa: E402
from tests.conftest import requires_db  # noqa: E402


def test_evidence_key_is_stable_across_wording_and_numbers():
    a = evidence_key("BHP", "production", "Production guidance cut by 12% at Olympic Dam")
    b = evidence_key("bhp", "Production", "Olympic Dam production guidance cut by 15 %")
    c = evidence_key("BHP", "cost", "Production guidance cut by 12% at Olympic Dam")
    assert a == b, "same finding, different wording/numbers -> same key"
    assert a != c, "different pillar -> different key"
    assert normalise_claim("The company's costs are rising") == normalise_claim("costs rising")


def test_ai_source_url_prefers_cited_document_else_deterministic():
    key = evidence_key("BHP", "cost", "Costs rising")
    assert ai_source_url("BHP", "cost", key, "https://asx.com.au/x.pdf") == "https://asx.com.au/x.pdf"
    synthetic = ai_source_url("BHP", "cost", key)
    assert synthetic == ai_source_url("BHP", "cost", key), "no time component"
    assert synthetic.startswith("claude-deep://BHP/cost/")


def test_duplicate_grouping_and_keeper_choice():
    rows = [
        {"id": 1, "prospect_id": "p", "pressure_type": "cost", "ticker": "X", "summary": "Costs are rising fast", "confidence_score": 0.7, "detected_at": "2026-01-02", "verification_status": "ai_generated"},
        {"id": 2, "prospect_id": "p", "pressure_type": "cost", "ticker": "X", "summary": "Fast-rising costs.", "confidence_score": 0.95, "detected_at": "2026-02-02", "verification_status": "ai_confirmed"},
        {"id": 3, "prospect_id": "p", "pressure_type": "people", "ticker": "X", "summary": "CEO resigned", "confidence_score": 0.7, "detected_at": "2026-01-02", "verification_status": "ai_generated"},
    ]
    groups = legacy_duplicate_groups(rows)
    assert len(groups) == 1 and {r["id"] for r in groups[0]} == {1, 2}
    assert choose_keeper(groups[0])["id"] == 2


@requires_db
def test_reanalysis_of_same_finding_is_a_revision_not_a_new_signal(db_conn, factory):
    p = factory.create()
    result = {
        "analysis_version": "claude-deep-v1", "tokens_used": 100,
        "validated_signals": [],
        "new_signals": [{"pressure_type": "cost", "strength": "strong", "summary": "Unit costs rising 20% year on year",
                         "reasoning": "r", "source_title": "Quarterly", "verification_status": "ai_generated"}],
        "refined_profile": {"likelihood_score": 6},
    }
    first = api._store_deep_analysis_result(db_conn, p["prospect_id"], p["listing_id"], p["ticker"], [], dict(result))
    second = api._store_deep_analysis_result(db_conn, p["prospect_id"], p["listing_id"], p["ticker"], [], dict(result))
    assert first == 1 and second == 0
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT revision_count, evidence_key, verification_status, prospect_score FROM pressure_signals ps JOIN prospect_matrix pm ON pm.id = ps.prospect_id WHERE ps.prospect_id = %s", (p["prospect_id"],))
        rows = cur.fetchall()
    assert len(rows) == 1 and rows[0]["revision_count"] == 2 and rows[0]["evidence_key"]
    with db_conn.cursor() as cur:
        cur.execute("SELECT prospect_score FROM prospect_matrix WHERE id = %s", (p["prospect_id"],))
        score_after_two = cur.fetchone()[0]
    api._store_deep_analysis_result(db_conn, p["prospect_id"], p["listing_id"], p["ticker"], [], dict(result))
    with db_conn.cursor() as cur:
        cur.execute("SELECT prospect_score FROM prospect_matrix WHERE id = %s", (p["prospect_id"],))
        assert cur.fetchone()[0] == score_after_two, "re-running must not inflate the score"


@requires_db
def test_unverified_ai_claim_is_stored_but_excluded_from_score(db_conn, factory):
    p = factory.create()
    result = {"analysis_version": "claude-deep-v1", "validated_signals": [], "refined_profile": {},
              "new_signals": [{"pressure_type": "production", "strength": "strong", "summary": "Plant halted for unspecified reasons",
                               "source_title": "Made up", "verification_status": "unverified"}]}
    api._store_deep_analysis_result(db_conn, p["prospect_id"], p["listing_id"], p["ticker"], [], result)
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT is_valid, verification_status FROM pressure_signals WHERE prospect_id = %s", (p["prospect_id"],))
        row = cur.fetchone()
        cur.execute("SELECT prospect_score, score_breakdown->>'signals_used' AS used FROM prospect_matrix WHERE id = %s", (p["prospect_id"],))
        pm = cur.fetchone()
    assert row["is_valid"] is False and row["verification_status"] == "unverified"
    assert float(pm["prospect_score"]) == 0 and pm["used"] == "0"


@requires_db
def test_ai_citation_of_a_rule_signal_url_confirms_instead_of_duplicating(db_conn, factory):
    p = factory.create()
    url = "https://announcements.asx.com.au/asxpdf/1.pdf"
    factory.add_signal(p["prospect_id"], pillar="cost", source_url=url, summary="Capital raising announced")
    result = {"analysis_version": "claude-deep-v1", "validated_signals": [], "refined_profile": {},
              "new_signals": [{"pressure_type": "cost", "strength": "strong", "summary": "Company raising capital to shore up balance sheet",
                               "source_title": "Capital raising announced", "source_url": url, "verification_status": "ai_generated"}]}
    inserted = api._store_deep_analysis_result(db_conn, p["prospect_id"], p["listing_id"], p["ticker"], [], result)
    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT COUNT(*) AS n, MAX(verification_status) AS vs FROM pressure_signals WHERE prospect_id = %s", (p["prospect_id"],))
        row = cur.fetchone()
    assert inserted == 0 and row["n"] == 1 and row["vs"] == "ai_confirmed"


@requires_db
def test_dedupe_script_report_apply_undo(db_conn, factory):
    import dedupe_ai_signals as dd
    p = factory.create()
    # Grouping is deliberately conservative: same content words only. The third
    # row is worded differently and must NOT be merged.
    for i, text in enumerate(["Unit costs rising sharply", "Unit costs are rising sharply.", "Sharp rise in unit costs"]):
        factory.add_signal(p["prospect_id"], pillar="cost", summary=text, source_url=f"claude-deep://{p['ticker']}/cost/legacy{i}",
                           verification_status="ai_generated")
    rows = [r for r in dd.load_ai_signals(db_conn) if r["ticker"] == p["ticker"]]
    report = dd.build_report(rows)
    assert len([r for r in report if r["role"] == "supersede"]) == 1
    outcome = dd.apply(db_conn, report)
    assert outcome == {"superseded": 1, "prospects_rescored": 1}
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM pressure_signals WHERE prospect_id = %s AND superseded_by IS NULL", (p["prospect_id"],))
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT score_breakdown->>'signals_used' FROM prospect_matrix WHERE id = %s", (p["prospect_id"],))
        assert cur.fetchone()[0] == "2"
    dd.undo(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM pressure_signals WHERE prospect_id = %s AND superseded_by IS NULL", (p["prospect_id"],))
        assert cur.fetchone()[0] == 3
