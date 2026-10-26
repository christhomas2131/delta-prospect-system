"""
Legacy AI-signal duplicate review (Phase 2).

Before Phase 2, every deep-analysis run stored its findings under a time-based
source URL, so re-running analysis on a company created the same finding again.
This script finds those duplicates, prints a report, and — only with --apply —
marks the extras as superseded (is_valid = FALSE, superseded_by = keeper) and
rescores the affected companies. Nothing is deleted; --undo reverses it.

    python scripts/dedupe_ai_signals.py                 # report only (default)
    python scripts/dedupe_ai_signals.py --csv out.csv   # report to CSV
    python scripts/dedupe_ai_signals.py --apply         # flag duplicates + rescore
    python scripts/dedupe_ai_signals.py --undo          # clear flags set by this script
    python scripts/dedupe_ai_signals.py --backfill-keys # only fill evidence_key on legacy rows
"""

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg2.extras import RealDictCursor  # noqa: E402

from db import connect  # noqa: E402
from evidence import choose_keeper, evidence_key, legacy_duplicate_groups  # noqa: E402

REASON = "legacy_ai_duplicate"


def load_ai_signals(conn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT ps.id, ps.prospect_id, ps.pressure_type::text AS pressure_type, ps.strength::text AS strength,
                   ps.summary, ps.source_url, ps.confidence_score, ps.detected_at, ps.is_valid,
                   ps.verification_status, ps.evidence_key, ps.superseded_by, l.ticker
            FROM pressure_signals ps
            JOIN prospect_matrix pm ON pm.id = ps.prospect_id
            JOIN asx_listings l ON l.id = pm.listing_id
            WHERE ps.source_url LIKE 'claude-deep://%%'
            ORDER BY ps.prospect_id, ps.pressure_type, ps.detected_at
        """)
        return [dict(r) for r in cur.fetchall()]


def backfill_keys(conn, rows: list[dict]) -> int:
    n = 0
    with conn.cursor() as cur:
        for r in rows:
            if r.get("evidence_key"):
                continue
            cur.execute("UPDATE pressure_signals SET evidence_key = %s WHERE id = %s",
                        (evidence_key(r["ticker"], r["pressure_type"], r["summary"]), r["id"]))
            n += cur.rowcount
    conn.commit()
    return n


def build_report(rows: list[dict]) -> list[dict]:
    report = []
    for group in legacy_duplicate_groups([r for r in rows if r["superseded_by"] is None]):
        keeper = choose_keeper(group)
        for r in group:
            report.append({
                "ticker": r["ticker"], "prospect_id": str(r["prospect_id"]), "pillar": r["pressure_type"],
                "signal_id": str(r["id"]), "role": "keep" if r["id"] == keeper["id"] else "supersede",
                "keeper_id": str(keeper["id"]), "strength": r["strength"], "confidence": r["confidence_score"],
                "detected_at": r["detected_at"].isoformat() if r["detected_at"] else "",
                "summary": (r["summary"] or "")[:160],
            })
    return report


def apply(conn, report: list[dict]) -> dict:
    extras = [r for r in report if r["role"] == "supersede"]
    prospects = {r["prospect_id"] for r in extras}
    with conn.cursor() as cur:
        for r in extras:
            cur.execute("""
                UPDATE pressure_signals
                SET is_valid = FALSE, superseded_by = %s, superseded_at = NOW(), supersede_reason = %s,
                    verification_status = 'superseded'
                WHERE id = %s AND superseded_by IS NULL
            """, (r["keeper_id"], REASON, r["signal_id"]))
        for pid in prospects:
            cur.execute("SELECT calculate_prospect_score(%s)", (pid,))
            cur.execute("SELECT calculate_lead_tier(%s)", (pid,))
    conn.commit()
    return {"superseded": len(extras), "prospects_rescored": len(prospects)}


def undo(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE pressure_signals
            SET is_valid = NULL, superseded_by = NULL, superseded_at = NULL, supersede_reason = NULL,
                verification_status = 'ai_generated'
            WHERE supersede_reason = %s RETURNING prospect_id
        """, (REASON,))
        prospects = {str(r[0]) for r in cur.fetchall()}
        for pid in prospects:
            cur.execute("SELECT calculate_prospect_score(%s)", (pid,))
            cur.execute("SELECT calculate_lead_tier(%s)", (pid,))
    conn.commit()
    return {"restored_prospects": len(prospects)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="flag duplicates as superseded and rescore")
    ap.add_argument("--undo", action="store_true", help="reverse flags set by this script")
    ap.add_argument("--backfill-keys", action="store_true", help="only backfill evidence_key on legacy AI rows")
    ap.add_argument("--csv", help="write the report to this CSV path")
    args = ap.parse_args(argv)

    conn = connect()
    try:
        if args.undo:
            print("Undo:", undo(conn))
            return 0
        rows = load_ai_signals(conn)
        filled = backfill_keys(conn, rows)
        print(f"Legacy AI signals: {len(rows)} (evidence_key backfilled on {filled})")
        if args.backfill_keys:
            return 0
        report = build_report(rows)
        groups = {r["keeper_id"] for r in report}
        extras = [r for r in report if r["role"] == "supersede"]
        print(f"Duplicate groups: {len(groups)}  rows that would be superseded: {len(extras)}")
        for r in report[:60]:
            print(f"  {r['ticker']:6} {r['pillar']:18} {r['role']:9} {r['strength']:8} {r['detected_at'][:10]}  {r['summary'][:70]}")
        if len(report) > 60:
            print(f"  ... {len(report) - 60} more rows (use --csv)")
        if args.csv:
            with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(report[0]) if report else ["ticker"])
                w.writeheader(); w.writerows(report)
            print("Report written to", args.csv)
        if args.apply:
            print("Apply:", apply(conn, report))
        elif extras:
            print("Dry run — nothing changed. Re-run with --apply to supersede the rows marked 'supersede'.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
