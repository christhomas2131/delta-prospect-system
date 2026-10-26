"""
Paid-analysis controls (Phase 3): spend ledger, monthly budget, evidence cache.

Environment
-----------
AI_MONTHLY_BUDGET_USD      0 (default) = unlimited; otherwise reservations that
                           would push the month over this ceiling raise BudgetExceeded.
AI_PRICE_INPUT_PER_M       USD per 1M input tokens  (default 3.00, claude-sonnet list price)
AI_PRICE_OUTPUT_PER_M      USD per 1M output tokens (default 15.00)
AI_DEFAULT_RESERVE_USD     Reservation taken before a call when no history exists (default 0.08)
AI_CACHE_MAX_AGE_DAYS      Reuse a stored analysis with identical evidence for this long (default 30)

Costs here are *estimates* from token counts and list prices. Reconcile against
the provider invoice monthly (see OPERATIONS.md) before trusting cost-per-lead.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor

logger = logging.getLogger("ai_budget")

MONTHLY_BUDGET_USD = float(os.getenv("AI_MONTHLY_BUDGET_USD", "0") or 0)
PRICE_INPUT_PER_M = float(os.getenv("AI_PRICE_INPUT_PER_M", "3.0"))
PRICE_OUTPUT_PER_M = float(os.getenv("AI_PRICE_OUTPUT_PER_M", "15.0"))
DEFAULT_RESERVE_USD = float(os.getenv("AI_DEFAULT_RESERVE_USD", "0.08"))
CACHE_MAX_AGE_DAYS = int(os.getenv("AI_CACHE_MAX_AGE_DAYS", "30"))


class BudgetExceeded(Exception):
    pass


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return round(input_tokens / 1e6 * PRICE_INPUT_PER_M + output_tokens / 1e6 * PRICE_OUTPUT_PER_M, 4)


def _period(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def month_spend(conn, period: str | None = None) -> dict:
    period = period or _period()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT reserved_usd, settled_usd FROM ai_budget_periods WHERE period = %s", (period,))
        row = cur.fetchone() or {"reserved_usd": 0, "settled_usd": 0}
    committed = float(row["reserved_usd"] or 0) + float(row["settled_usd"] or 0)
    return {"period": period, "reserved_usd": float(row["reserved_usd"] or 0),
            "settled_usd": float(row["settled_usd"] or 0), "committed_usd": round(committed, 4),
            "budget_usd": MONTHLY_BUDGET_USD or None,
            "remaining_usd": None if not MONTHLY_BUDGET_USD else round(MONTHLY_BUDGET_USD - committed, 4)}


def average_cost_per_company(conn) -> float:
    with conn.cursor() as cur:
        cur.execute("SELECT AVG(estimated_cost_usd) FROM ai_usage_ledger WHERE status = 'settled' AND estimated_cost_usd > 0")
        row = cur.fetchone()
    return float(row[0]) if row and row[0] else DEFAULT_RESERVE_USD


def estimate_batch(conn, companies: int = 10) -> dict:
    per = average_cost_per_company(conn)
    spend = month_spend(conn)
    total = round(per * companies, 4)
    within = spend["remaining_usd"] is None or total <= spend["remaining_usd"]
    return {"companies": companies, "est_per_company_usd": round(per, 4), "est_total_usd": total,
            **spend, "within_budget": within,
            "note": "Estimate from token counts and list prices; actual billing may differ."}


# ---------------------------------------------------------------------------
# Reservation / settlement
# ---------------------------------------------------------------------------

def reserve(conn, prospect_id: str | None, *, job_id: str | None = None, amount_usd: float | None = None) -> dict:
    """Lock the month row, check the ceiling, record a reservation."""
    amount = amount_usd if amount_usd is not None else average_cost_per_company(conn)
    period = _period()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("INSERT INTO ai_budget_periods (period) VALUES (%s) ON CONFLICT (period) DO NOTHING", (period,))
        cur.execute("SELECT reserved_usd, settled_usd FROM ai_budget_periods WHERE period = %s FOR UPDATE", (period,))
        row = cur.fetchone()
        committed = float(row["reserved_usd"]) + float(row["settled_usd"])
        if MONTHLY_BUDGET_USD and committed + amount > MONTHLY_BUDGET_USD:
            conn.rollback()
            raise BudgetExceeded(
                f"AI budget reached: ${committed:.2f} committed of ${MONTHLY_BUDGET_USD:.2f} for {period}; "
                f"a further ${amount:.2f} would exceed it. Raise AI_MONTHLY_BUDGET_USD or wait for next month."
            )
        cur.execute("UPDATE ai_budget_periods SET reserved_usd = reserved_usd + %s, updated_at = NOW() WHERE period = %s",
                    (amount, period))
        cur.execute(
            """INSERT INTO ai_usage_ledger (prospect_id, job_id, status, reserved_usd)
               VALUES (%s, %s, 'reserved', %s) RETURNING id""",
            (prospect_id, job_id, amount),
        )
        ledger_id = str(cur.fetchone()["id"])
    conn.commit()
    return {"id": ledger_id, "period": period, "reserved_usd": amount}


def _finish(conn, reservation: dict, *, status: str, actual_usd: float, **fields) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE ai_budget_periods
               SET reserved_usd = GREATEST(reserved_usd - %s, 0), settled_usd = settled_usd + %s, updated_at = NOW()
               WHERE period = %s""",
            (reservation["reserved_usd"], actual_usd if status == "settled" else 0, reservation["period"]),
        )
        cur.execute(
            """UPDATE ai_usage_ledger SET status = %s, settled_at = NOW(), estimated_cost_usd = %s,
               input_tokens = %s, output_tokens = %s, model_name = %s, analysis_version = %s, evidence_hash = %s
               WHERE id = %s""",
            (status, actual_usd, fields.get("input_tokens"), fields.get("output_tokens"),
             fields.get("model_name"), fields.get("analysis_version"), fields.get("evidence_hash"), reservation["id"]),
        )
    conn.commit()


def settle(conn, reservation: dict, result: dict, *, status: str = "settled") -> float:
    in_tok = int(result.get("input_tokens") or 0)
    out_tok = int(result.get("output_tokens") or 0)
    if not in_tok and not out_tok and result.get("tokens_used"):
        # Legacy result with a combined count: assume ~80% input.
        in_tok = int(result["tokens_used"] * 0.8)
        out_tok = int(result["tokens_used"]) - in_tok
    cost = estimate_cost(in_tok, out_tok)
    _finish(conn, reservation, status=status, actual_usd=cost, input_tokens=in_tok, output_tokens=out_tok,
            model_name=result.get("model_name"), analysis_version=result.get("analysis_version"),
            evidence_hash=result.get("evidence_hash"))
    return cost


def release(conn, reservation: dict) -> None:
    _finish(conn, reservation, status="released", actual_usd=0.0)


# ---------------------------------------------------------------------------
# Evidence cache
# ---------------------------------------------------------------------------

def evidence_hash_for_prospect(conn, prospect_id: str, listing_id: str, analysis_version: str = "v2") -> str:
    """Hash of everything the analysis would read: active signals + fetched documents."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id::text, strength::text, COALESCE(is_valid::text, 'null')
               FROM pressure_signals WHERE prospect_id = %s AND superseded_by IS NULL ORDER BY id""",
            (prospect_id,),
        )
        signals = cur.fetchall()
        cur.execute(
            """SELECT source_url, COALESCE(content_hash, '') FROM announcement_documents
               WHERE listing_id = %s AND fetch_status = 'fetched' ORDER BY source_url""",
            (listing_id,),
        )
        docs = cur.fetchall()
    material = json.dumps({"v": analysis_version, "signals": signals, "docs": docs}, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def find_cached_run(conn, prospect_id: str, evidence_hash: str) -> dict | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT id, output_json, completed_at FROM prospect_intelligence_runs
               WHERE prospect_id = %s AND evidence_hash = %s AND status = 'completed'
                 AND completed_at > NOW() - (%s || ' days')::interval
               ORDER BY completed_at DESC LIMIT 1""",
            (prospect_id, evidence_hash, str(CACHE_MAX_AGE_DAYS)),
        )
        row = cur.fetchone()
    if not row:
        return None
    row = dict(row)
    if isinstance(row.get("output_json"), str):
        row["output_json"] = json.loads(row["output_json"])
    return row


def ledger_summary(conn, months: int = 6) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT to_char(created_at, 'YYYY-MM') AS period,
                      COUNT(*) FILTER (WHERE status = 'settled') AS calls,
                      COUNT(DISTINCT prospect_id) FILTER (WHERE status = 'settled') AS companies,
                      ROUND(COALESCE(SUM(estimated_cost_usd) FILTER (WHERE status = 'settled'), 0), 4) AS spend_usd,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
                      COUNT(*) FILTER (WHERE status = 'released') AS released
               FROM ai_usage_ledger
               WHERE created_at >= date_trunc('month', NOW()) - (%s || ' months')::interval
               GROUP BY 1 ORDER BY 1 DESC""",
            (str(months),),
        )
        return [dict(r) for r in cur.fetchall()]
