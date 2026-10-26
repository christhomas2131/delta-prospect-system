"""
PostgreSQL-backed job queue.

Every long-running action (ASX refresh, batch enrichment, contact backfill,
deep analysis) is a row in ``background_jobs``. Progress is persisted, so a
restart never loses track of what was running, and the dashboard can poll the
database instead of process memory.

Typical use::

    job = enqueue(conn, "asx_refresh", {}, idempotency_key="asx_refresh")
    ...
    claimed = claim_next(conn, worker_id, ["asx_refresh", ...])
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

from psycopg2.extras import Json, RealDictCursor

logger = logging.getLogger("jobs")

LEASE_SECONDS = 90
ACTIVE = ("queued", "running")
TERMINAL = ("succeeded", "failed", "cancelled")


class JobCancelled(Exception):
    """Raised inside a handler when the operator asked for cancellation."""


def _row(cur) -> Optional[dict]:
    row = cur.fetchone()
    return dict(row) if row else None


def enqueue(conn, job_type: str, params: dict | None = None, *, idempotency_key: str | None = None,
            triggered_by: str = "api", priority: int = 5, max_attempts: int = 2) -> dict:
    """Insert a job. If an active job already holds ``idempotency_key`` that job is
    returned instead (``existing=True``) and nothing new is created."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if idempotency_key:
            cur.execute(
                "SELECT * FROM background_jobs WHERE idempotency_key = %s AND status IN %s "
                "ORDER BY created_at DESC LIMIT 1",
                (idempotency_key, ACTIVE),
            )
            existing = _row(cur)
            if existing:
                existing["existing"] = True
                return existing
        try:
            cur.execute(
                """INSERT INTO background_jobs (job_type, params, idempotency_key, triggered_by, priority, max_attempts)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                (job_type, Json(params or {}), idempotency_key, triggered_by, priority, max_attempts),
            )
            job = _row(cur)
            conn.commit()
            job["existing"] = False
            return job
        except Exception as exc:  # unique-index race: another request won
            conn.rollback()
            if idempotency_key and "uq_background_jobs_active_key" in str(exc):
                cur.execute(
                    "SELECT * FROM background_jobs WHERE idempotency_key = %s AND status IN %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (idempotency_key, ACTIVE),
                )
                existing = _row(cur)
                if existing:
                    existing["existing"] = True
                    return existing
            raise


def claim_next(conn, worker_id: str, job_types: Iterable[str] | None = None) -> Optional[dict]:
    """Atomically claim the next queued job (FOR UPDATE SKIP LOCKED)."""
    types = tuple(job_types or ())
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        type_sql = "AND job_type IN %s" if types else ""
        params = [types] if types else []
        cur.execute(
            f"""
            WITH next_job AS (
                SELECT id FROM background_jobs
                WHERE status = 'queued' AND cancel_requested = FALSE {type_sql}
                ORDER BY priority, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE background_jobs j
            SET status = 'running', worker_id = %s, attempts = j.attempts + 1,
                started_at = COALESCE(j.started_at, NOW()), heartbeat_at = NOW(),
                lease_expires_at = NOW() + (%s || ' seconds')::interval, updated_at = NOW()
            FROM next_job WHERE j.id = next_job.id
            RETURNING j.*
            """,
            params + [worker_id, str(LEASE_SECONDS)],
        )
        job = _row(cur)
    conn.commit()
    return job


def heartbeat(conn, job_id: str) -> bool:
    """Extend the lease. Returns False if the job was cancelled/reassigned."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE background_jobs SET heartbeat_at = NOW(),
               lease_expires_at = NOW() + (%s || ' seconds')::interval, updated_at = NOW()
               WHERE id = %s AND status = 'running' RETURNING cancel_requested""",
            (str(LEASE_SECONDS), job_id),
        )
        row = cur.fetchone()
    conn.commit()
    return bool(row) and not row[0]


def update_progress(conn, job_id: str, progress: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE background_jobs SET progress = %s, updated_at = NOW() WHERE id = %s",
            (Json(progress), job_id),
        )
    conn.commit()


def is_cancel_requested(conn, job_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT cancel_requested FROM background_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
    return bool(row and row[0])


def complete(conn, job_id: str, result: dict | None = None, progress: dict | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE background_jobs SET status = 'succeeded', result = %s,
               progress = COALESCE(%s, progress), finished_at = NOW(), updated_at = NOW()
               WHERE id = %s""",
            (Json(result or {}), Json(progress) if progress is not None else None, job_id),
        )
    conn.commit()


def fail(conn, job_id: str, error: str, *, retry: bool = True, progress: dict | None = None) -> str:
    """Mark failed, or re-queue when attempts remain. Returns the new status."""
    with conn.cursor() as cur:
        cur.execute("SELECT attempts, max_attempts FROM background_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        requeue = bool(row) and retry and row[0] < row[1]
        new_status = "queued" if requeue else "failed"
        cur.execute(
            """UPDATE background_jobs SET status = %s, error = %s,
               progress = COALESCE(%s, progress), worker_id = NULL, lease_expires_at = NULL,
               finished_at = CASE WHEN %s = 'failed' THEN NOW() ELSE NULL END, updated_at = NOW()
               WHERE id = %s""",
            (new_status, error[:2000], Json(progress) if progress is not None else None, new_status, job_id),
        )
    conn.commit()
    return new_status


def cancel(conn, job_id: str, progress: dict | None = None) -> Optional[dict]:
    """Queued jobs are cancelled immediately; running jobs get cancel_requested
    and the handler stops at its next checkpoint."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """UPDATE background_jobs
               SET cancel_requested = TRUE,
                   status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
                   finished_at = CASE WHEN status = 'queued' THEN NOW() ELSE finished_at END,
                   progress = COALESCE(%s, progress), updated_at = NOW()
               WHERE id = %s AND status IN ('queued','running') RETURNING *""",
            (Json(progress) if progress is not None else None, job_id),
        )
        job = _row(cur)
    conn.commit()
    return job


def mark_cancelled(conn, job_id: str, progress: dict | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE background_jobs SET status = 'cancelled', finished_at = NOW(),
               progress = COALESCE(%s, progress), updated_at = NOW() WHERE id = %s""",
            (Json(progress) if progress is not None else None, job_id),
        )
    conn.commit()


def reclaim_expired(conn) -> int:
    """Running jobs whose lease expired (worker died) go back to the queue, or fail
    when they have used all attempts."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE background_jobs
               SET status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
                   error = COALESCE(error, '') || ' [lease expired: worker stopped responding]',
                   worker_id = NULL, lease_expires_at = NULL,
                   finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE NOW() END,
                   updated_at = NOW()
               WHERE status = 'running' AND lease_expires_at < NOW()""",
        )
        n = cur.rowcount
    conn.commit()
    if n:
        logger.warning("reclaimed %d expired job lease(s)", n)
    return n


def get_job(conn, job_id: str) -> Optional[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM background_jobs WHERE id = %s", (job_id,))
        return _row(cur)


def latest_job(conn, job_type: str, *, param_match: dict | None = None) -> Optional[dict]:
    """Most recent job of a type, optionally filtered by params (e.g. prospect_id)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if param_match:
            cur.execute(
                "SELECT * FROM background_jobs WHERE job_type = %s AND params @> %s "
                "ORDER BY created_at DESC LIMIT 1",
                (job_type, Json(param_match)),
            )
        else:
            cur.execute(
                "SELECT * FROM background_jobs WHERE job_type = %s ORDER BY created_at DESC LIMIT 1",
                (job_type,),
            )
        return _row(cur)


def list_jobs(conn, *, job_type: str | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
    where, params = [], []
    if job_type:
        where.append("job_type = %s")
        params.append(job_type)
    if status:
        where.append("status = %s")
        params.append(status)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"SELECT * FROM background_jobs {where_sql} ORDER BY created_at DESC LIMIT %s",
            params + [limit],
        )
        return [dict(r) for r in cur.fetchall()]


def queue_stats(conn) -> dict:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'queued')  AS queued,
                COUNT(*) FILTER (WHERE status = 'running') AS running,
                COUNT(*) FILTER (WHERE status = 'failed'   AND finished_at > NOW() - INTERVAL '24 hours') AS failed_24h,
                COUNT(*) FILTER (WHERE status = 'succeeded' AND finished_at > NOW() - INTERVAL '24 hours') AS succeeded_24h,
                COUNT(*) FILTER (WHERE status = 'running' AND lease_expires_at < NOW()) AS stale_leases,
                EXTRACT(EPOCH FROM (NOW() - MIN(created_at) FILTER (WHERE status = 'queued')))::int AS oldest_queued_seconds,
                MAX(heartbeat_at) AS last_heartbeat_at
            FROM background_jobs
        """)
        return dict(cur.fetchone())


class JobProgress:
    """Dict-like progress tracker that persists to the job row (throttled) and
    exposes cooperative cancellation. Handlers keep using plain keys so the
    JSON shape the frontend polls is unchanged."""

    def __init__(self, conn, job: dict, *, min_interval: float = 0.75):
        self.conn = conn
        self.job_id = str(job["id"])
        self.data: dict = dict(job.get("progress") or {})
        self._min_interval = min_interval
        self._last_flush = 0.0

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        self.flush()

    def get(self, key, default=None):
        return self.data.get(key, default)

    def update(self, *args, **kwargs):
        self.data.update(*args, **kwargs)
        self.flush()

    def flush(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self._last_flush < self._min_interval:
            return
        self._last_flush = now
        try:
            update_progress(self.conn, self.job_id, self.data)
        except Exception:  # progress is best-effort; never kill the job for it
            self.conn.rollback()
            logger.debug("progress flush failed for %s", self.job_id, exc_info=True)

    def checkpoint(self):
        """Call between units of work: flushes progress, extends the lease and
        raises JobCancelled if the operator asked to stop."""
        self.flush(force=True)
        if not heartbeat(self.conn, self.job_id):
            raise JobCancelled(f"job {self.job_id} cancelled")

    def cancelled(self) -> bool:
        return is_cancel_requested(self.conn, self.job_id)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_public(job: dict | None) -> dict | None:
    """JSON-safe view of a job row."""
    if not job:
        return None
    out = {}
    for k, v in job.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif k in ("params", "progress", "result") and isinstance(v, str):
            out[k] = json.loads(v)
        else:
            out[k] = str(v) if k == "id" else v
    return out
