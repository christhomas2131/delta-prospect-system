"""
Evaluate scoring policies against an operator-labelled set (Phase 3 gate).

Labelling workflow
------------------
1. python scripts/evaluate_ranking.py --export evaluation/evaluation_set.csv
   (writes 40-60 candidate companies: stratified by sector, size band and
   signal volume, plus some DQ'd rows as negatives; keeps existing labels).
2. The business owner fills `label` (1 = merits analyst follow-up, 0 = not)
   and optionally `label_note`. Rows left blank are ignored.
3. python scripts/evaluate_ranking.py evaluation/evaluation_set.csv
   prints precision@K, recall and label coverage for v1 vs v2.

The sample is small; treat the numbers as an acceptance check (e.g. ">= 16 of
the top 20 merit follow-up"), not statistical proof.
"""

import argparse
import csv
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg2.extras import RealDictCursor  # noqa: E402

from db import connect  # noqa: E402

FIELDS = ["ticker", "company_name", "gics_sector", "market_cap_band", "status", "lead_tier",
          "signals", "score_v1", "score_v2", "label", "label_note"]


def _band(cents):
    if not cents:
        return "unknown"
    aud = cents / 100
    return "<50M" if aud < 50e6 else "50M-500M" if aud < 500e6 else ">500M"


def export(conn, path: Path, size: int = 50, seed: int = 7) -> int:
    existing = {}
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            existing = {r["ticker"]: r for r in csv.DictReader(fh)}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT l.ticker, l.company_name, l.gics_sector, l.market_cap_aud, pm.status::text AS status, pm.lead_tier,
                   COALESCE(pm.score_v1, 0) AS score_v1, COALESCE(pm.score_v2, 0) AS score_v2,
                   (SELECT COUNT(*) FROM pressure_signals ps WHERE ps.prospect_id = pm.id) AS signals
            FROM prospect_matrix pm JOIN asx_listings l ON l.id = pm.listing_id
            WHERE l.is_active AND l.is_target_sector
        """)
        rows = [dict(r) for r in cur.fetchall()]
    rng = random.Random(seed)
    strata: dict[tuple, list[dict]] = {}
    for r in rows:
        r["market_cap_band"] = _band(r["market_cap_aud"])
        key = (r["gics_sector"], r["market_cap_band"], "dq" if r["status"] == "disqualified" else "signals" if r["signals"] else "none")
        strata.setdefault(key, []).append(r)
    picked: list[dict] = []
    keys = sorted(strata)
    while len(picked) < size and any(strata.values()):
        for key in keys:
            bucket = strata[key]
            if bucket and len(picked) < size:
                picked.append(bucket.pop(rng.randrange(len(bucket))))
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in sorted(picked, key=lambda r: -float(r["score_v1"])):
            prev = existing.get(r["ticker"], {})
            w.writerow({**{k: r.get(k, "") for k in FIELDS}, "label": prev.get("label", ""), "label_note": prev.get("label_note", "")})
    return len(picked)


def evaluate(path: Path, k: int = 20) -> dict:
    with path.open(encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("label") or "").strip() in ("0", "1")]
    if not rows:
        return {"labelled": 0}
    positives = sum(int(r["label"]) for r in rows)
    out = {"labelled": len(rows), "positives": positives, "k": min(k, len(rows))}
    for policy in ("v1", "v2"):
        ranked = sorted(rows, key=lambda r: -float(r.get(f"score_{policy}") or 0))
        top = ranked[: out["k"]]
        hits = sum(int(r["label"]) for r in top)
        out[policy] = {"precision_at_k": round(hits / out["k"], 3), "hits": hits,
                       "recall_at_k": round(hits / positives, 3) if positives else None}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default="evaluation/evaluation_set.csv")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--size", type=int, default=50)
    ap.add_argument("--k", type=int, default=20)
    args = ap.parse_args(argv)
    path = Path(args.path)
    if args.export:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect()
        try:
            n = export(conn, path, args.size)
        finally:
            conn.close()
        print(f"Wrote {n} candidates to {path}. Fill the 'label' column (1/0) and re-run without --export.")
        return 0
    if not path.exists():
        print(f"{path} not found — run with --export first.")
        return 1
    result = evaluate(path, args.k)
    if not result["labelled"]:
        print("No labelled rows yet (label column must be 0 or 1).")
        return 0
    print(f"Labelled rows: {result['labelled']} (positives {result['positives']}), K={result['k']}")
    for policy in ("v1", "v2"):
        r = result[policy]
        print(f"  {policy}: precision@K {r['precision_at_k']:.2f} ({r['hits']}/{result['k']})  recall@K {r['recall_at_k']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
