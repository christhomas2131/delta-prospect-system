"""
Operational events and alerts.

Every noteworthy operational event is written to ``ops_events``. Events with
severity warning/error are also POSTed to ``ALERT_WEBHOOK_URL`` when that is
set (Slack/Teams-compatible ``{"text": ...}`` payload). Without a webhook the
events are still visible on the Ops page and in ``GET /api/ops/events``.
"""

import json
import logging
import os
from datetime import datetime, timezone

import httpx
from psycopg2.extras import Json, RealDictCursor

logger = logging.getLogger("alerts")

ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()
AI_MONTHLY_BUDGET_USD = float(os.getenv("AI_MONTHLY_BUDGET_USD", "0") or 0)


def notify(conn, event_type: str, message: str, *, payload: dict | None = None, severity: str = "warning") -> dict | None:
    """Record an ops event and (for warning/error) push it to the webhook."""
    row = None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO ops_events (event_type, severity, message, payload)
                   VALUES (%s, %s, %s, %s) RETURNING id, created_at""",
                (event_type, severity, message[:2000], Json(payload or {})),
            )
            row = dict(cur.fetchone())
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("could not record ops event %s", event_type)
    log = logger.error if severity == "error" else logger.warning if severity == "warning" else logger.info
    log("ops event %s: %s", event_type, message)
    if severity in ("warning", "error") and ALERT_WEBHOOK_URL:
        _post_webhook(f"[{severity.upper()}] {event_type}: {message}")
    return row


def _post_webhook(text: str) -> None:
    try:
        httpx.post(ALERT_WEBHOOK_URL, json={"text": text}, timeout=5)
    except Exception as exc:
        logger.warning("alert webhook failed: %s", exc)


def _recent_event_exists(conn, event_type: str, hours: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ops_events WHERE event_type = %s AND created_at > NOW() - (%s || ' hours')::interval LIMIT 1",
            (event_type, str(hours)),
        )
        return cur.fetchone() is not None


def check_alert_conditions(conn) -> list[str]:
    """Hourly sweep. Each condition alerts at most once per 24h."""
    fired: list[str] = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT
              (SELECT MAX(completed_at) FROM refresh_runs WHERE status = 'completed') AS last_ok_refresh,
              (SELECT COUNT(*) FROM refresh_runs WHERE status = 'failed'
                  AND triggered_by = 'scheduled_auto_refresh' AND started_at > NOW() - INTERVAL '24 hours') AS failed_scheduled_24h,
              (SELECT COUNT(*) FROM background_jobs WHERE status = 'running' AND lease_expires_at < NOW() - INTERVAL '10 minutes') AS stuck_jobs,
              (SELECT COUNT(*) FROM background_jobs WHERE status = 'queued' AND created_at < NOW() - INTERVAL '6 hours') AS old_queued
        """)
        snap = dict(cur.fetchone())
        spend = 0.0
        try:
            cur.execute("""
                SELECT COALESCE(SUM(estimated_cost_usd), 0) AS spend FROM ai_usage_ledger
                WHERE created_at >= date_trunc('month', NOW()) AND status IN ('settled','reserved')
            """)
            spend = float(cur.fetchone()["spend"] or 0)
        except Exception:
            conn.rollback()

    def fire(event_type, message, payload=None, severity="warning"):
        if not _recent_event_exists(conn, event_type, 24):
            notify(conn, event_type, message, payload=payload, severity=severity)
            fired.append(event_type)

    if snap["failed_scheduled_24h"]:
        fire("scheduled_refresh_failed", f"{snap['failed_scheduled_24h']} scheduled ASX refresh(es) failed in the last 24h", severity="error")
    last_ok = snap["last_ok_refresh"]
    if last_ok is None or (datetime.now(timezone.utc) - last_ok).total_seconds() > 48 * 3600:
        fire("refresh_stale", "No successful ASX refresh in the last 48 hours", {"last_ok_refresh": last_ok.isoformat() if last_ok else None})
    if snap["stuck_jobs"]:
        fire("jobs_stuck", f"{snap['stuck_jobs']} job(s) have an expired lease for >10 minutes (worker down?)", severity="error")
    if snap["old_queued"]:
        fire("jobs_backlog", f"{snap['old_queued']} job(s) queued for more than 6 hours — is the worker running?")
    if AI_MONTHLY_BUDGET_USD > 0 and spend >= 0.8 * AI_MONTHLY_BUDGET_USD:
        fire("ai_budget_threshold", f"AI spend this month ${spend:.2f} is at {spend / AI_MONTHLY_BUDGET_USD:.0%} of the ${AI_MONTHLY_BUDGET_USD:.0f} budget",
             {"spend_usd": spend, "budget_usd": AI_MONTHLY_BUDGET_USD})
    return fired


def recent_events(conn, limit: int = 50) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM ops_events ORDER BY created_at DESC LIMIT %s", (limit,))
        return [dict(r) for r in cur.fetchall()]
