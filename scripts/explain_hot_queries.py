"""
Measure the hot read paths before/after tuning (Phase 5).

Runs EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) on the queries the UI hits most
and prints execution time + whether sequential scans remain. Read-only.

    python scripts/explain_hot_queries.py
    python scripts/explain_hot_queries.py --json   # raw plans
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import connect  # noqa: E402

QUERIES = {
    "lead_matrix_default_page": """
        SELECT pm.id, l.ticker, pm.prospect_score,
               COUNT(ps.id) AS total_signals,
               COUNT(ps.id) FILTER (WHERE ps.strength = 'strong') AS strong_signals,
               EXISTS(SELECT 1 FROM pitchbook_snapshots pb WHERE pb.prospect_id = pm.id) AS has_snapshot
        FROM prospect_matrix pm
        JOIN asx_listings l ON l.id = pm.listing_id
        LEFT JOIN pressure_signals ps ON ps.prospect_id = pm.id
        WHERE l.is_active = TRUE AND l.is_target_sector = TRUE
        GROUP BY pm.id, l.id
        ORDER BY pm.prospect_score DESC NULLS LAST LIMIT 50 OFFSET 0
    """,
    "leaderboard_exclude_dq": """
        SELECT pm.id FROM prospect_matrix pm JOIN asx_listings l ON l.id = pm.listing_id
        WHERE l.is_active AND l.is_target_sector AND pm.status NOT IN ('disqualified','archived')
        ORDER BY pm.prospect_score DESC NULLS LAST LIMIT 10
    """,
    "search_ilike": """
        SELECT id, ticker FROM asx_listings
        WHERE is_active AND is_target_sector AND (company_name ILIKE '%min%' OR ticker ILIKE '%min%')
        ORDER BY CASE WHEN ticker ILIKE 'min%' THEN 0 ELSE 1 END, market_cap_aud DESC NULLS LAST LIMIT 20
    """,
    "review_queue_new_evidence": """
        SELECT pm.id, COUNT(ps.id) FROM prospect_matrix pm
        JOIN pressure_signals ps ON ps.prospect_id = pm.id
        WHERE ps.detected_at > NOW() - INTERVAL '7 days' AND ps.superseded_by IS NULL
        GROUP BY pm.id
    """,
    "jobs_latest_refresh": """
        SELECT * FROM background_jobs WHERE job_type = 'asx_refresh' ORDER BY created_at DESC LIMIT 1
    """,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    conn = connect()
    conn.set_session(readonly=True, autocommit=True)
    try:
        with conn.cursor() as cur:
            for name, sql in QUERIES.items():
                cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
                plan = cur.fetchone()[0][0]
                if args.json:
                    print(json.dumps({name: plan}, indent=1, default=str))
                    continue
                seq = json.dumps(plan).count('"Seq Scan"')
                print(f"{name:32} exec {plan['Execution Time']:8.2f} ms  plan {plan['Planning Time']:6.2f} ms  seq scans: {seq}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
