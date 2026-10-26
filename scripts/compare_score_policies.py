"""
Shadow comparison of scoring policies v1 (current default) and v2 (explainable).

Both scores are stored on every prospect whenever it is scored. This report
recomputes them for all active target prospects, then shows rank movement so
the operator can review the biggest changes before switching the displayed
policy with PUT /api/settings/score-policy.

    python scripts/compare_score_policies.py                 # recompute + print top movers
    python scripts/compare_score_policies.py --csv out.csv   # full table to CSV
    python scripts/compare_score_policies.py --no-recompute  # use stored values only
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg2.extras import RealDictCursor  # noqa: E402

from db import connect  # noqa: E402


def recompute(conn, limit: int | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT pm.id FROM prospect_matrix pm JOIN asx_listings l ON l.id = pm.listing_id
            WHERE l.is_active AND l.is_target_sector ORDER BY pm.prospect_score DESC NULLS LAST
            {'LIMIT %s' if limit else ''}
        """, (limit,) if limit else ())
        ids = [r[0] for r in cur.fetchall()]
        for pid in ids:
            cur.execute("SELECT calculate_prospect_score(%s)", (pid,))
    conn.commit()
    return len(ids)


def load(conn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT l.ticker, l.company_name, l.gics_sector, pm.lead_tier, pm.status,
                   COALESCE(pm.score_v1, 0) AS score_v1, COALESCE(pm.score_v2, 0) AS score_v2,
                   pm.score_breakdown->>'days_since_latest_evidence' AS days_since_latest_evidence,
                   pm.score_breakdown->>'signals_used' AS signals_used
            FROM prospect_matrix pm JOIN asx_listings l ON l.id = pm.listing_id
            WHERE l.is_active AND l.is_target_sector AND pm.status NOT IN ('disqualified','archived')
        """)
        rows = [dict(r) for r in cur.fetchall()]
    for key in ("score_v1", "score_v2"):
        for rank, row in enumerate(sorted(rows, key=lambda r: -float(r[key])), start=1):
            row[f"rank_{key[-2:]}"] = rank
    for row in rows:
        row["rank_move"] = row["rank_v1"] - row["rank_v2"]  # positive = moved up under v2
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv")
    ap.add_argument("--no-recompute", action="store_true")
    ap.add_argument("--limit", type=int, help="recompute only the top N by current score (faster)")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args(argv)
    conn = connect()
    try:
        if not args.no_recompute:
            print(f"Recomputed {recompute(conn, args.limit)} prospects (v1 and v2 stored side by side).")
        rows = load(conn)
        if not rows:
            print("No active target prospects to compare.")
            return 0
        top_v1 = sorted(rows, key=lambda r: r["rank_v1"])[:args.top]
        top_v2 = sorted(rows, key=lambda r: r["rank_v2"])[:args.top]
        overlap = {r["ticker"] for r in top_v1} & {r["ticker"] for r in top_v2}
        print(f"\nTop-{args.top} overlap between v1 and v2: {len(overlap)}/{args.top}")
        print(f"\n{'Ticker':7}{'v1':>8}{'rank':>6}{'v2':>8}{'rank':>6}{'move':>6}  {'tier':9}{'age(d)':>7}  company")
        for r in sorted(rows, key=lambda r: -abs(r["rank_move"]))[:args.top]:
            print(f"{r['ticker']:7}{float(r['score_v1']):8.2f}{r['rank_v1']:6d}{float(r['score_v2']):8.2f}{r['rank_v2']:6d}"
                  f"{r['rank_move']:+6d}  {r['lead_tier'] or '':9}{str(r['days_since_latest_evidence'] or '-'):>7}  {r['company_name'][:40]}")
        if args.csv:
            with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader(); w.writerows(sorted(rows, key=lambda r: r["rank_v2"]))
            print("\nFull table written to", args.csv)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
