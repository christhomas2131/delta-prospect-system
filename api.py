"""
Delta Prospect System — REST API
==================================
FastAPI backend serving the Prospect Matrix dashboard.

Endpoints:
    GET  /api/prospects              - List prospects with filtering/sorting
    GET  /api/prospects/{id}         - Get single prospect with signals
    PATCH /api/prospects/{id}        - Update prospect status/notes
    GET  /api/sectors                - Sector summary stats
    GET  /api/stats                  - Dashboard overview stats
    POST /api/refresh                - Queue an ASX data refresh job
    GET  /api/refresh/history        - Recent refresh runs
    POST /api/enrich/{ticker}        - Queue single company enrichment
    GET  /api/jobs                   - Background job queue
    GET  /api/search                 - Search companies by name/ticker

Usage:
    uvicorn api:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import sys
import logging
import time
import threading
import uuid
from datetime import datetime, timezone, date
from typing import Callable, Optional
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

import csv
import io
import json

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from psycopg2 import pool
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import jobs
import migrations
import worker as job_worker
from db import DB_CONFIG
from jobs import JobCancelled

# ---------------------------------------------------------------------------
# In-memory API key store (not persisted to DB — intentional)
# ---------------------------------------------------------------------------

_api_key_store: dict = {"key": None, "valid": False, "source": None}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}

PORT = int(os.getenv("PORT", "8000"))
APP_ENV = os.getenv("APP_ENV") or ("production" if os.getenv("RAILWAY_ENVIRONMENT") else "development")
APP_VERSION = os.getenv("APP_VERSION") or os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:12] or "dev"

# Auth (optional — set both to enable). Production without auth is allowed but
# loudly warned about (operator decision, Oct 2026).
AUTH_USER = os.getenv("AUTH_USER")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD")
AUTH_CONFIGURED = bool(AUTH_USER and AUTH_PASSWORD)

# CORS (comma-separated origins, or * for dev)
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

# Cron secret (protects /api/cron/enrich-all)
CRON_SECRET = os.getenv("CRON_SECRET", "")
AUTO_ASX_REFRESH_ENABLED = _env_flag("AUTO_ASX_REFRESH_ENABLED")
AUTO_ASX_REFRESH_TIMEZONE = os.getenv("AUTO_ASX_REFRESH_TIMEZONE", "Australia/Sydney")
AUTO_ASX_REFRESH_HOUR = int(os.getenv("AUTO_ASX_REFRESH_HOUR", "9"))
AUTO_ASX_REFRESH_MINUTE = int(os.getenv("AUTO_ASX_REFRESH_MINUTE", "0"))

# Process roles. With one container (Railway default) the API process also runs
# the worker thread and the scheduler. When scaling to several API replicas set
# RUN_WORKER/SCHEDULER_ENABLED=false on all but one, or run `python worker.py`.
RUN_WORKER = _env_flag("RUN_WORKER")
SCHEDULER_ENABLED = _env_flag("SCHEDULER_ENABLED")
AUTO_MIGRATE = _env_flag("AUTO_MIGRATE")
# insert_missing (default): seed snapshots/*.json only for companies without a
# snapshot. overwrite: refresh rows that were themselves seeded; analyst-edited
# rows (enriched_by='manual') are never touched.
SNAPSHOT_SEED_MODE = os.getenv("SNAPSHOT_SEED_MODE", "insert_missing").strip().lower()

# Static files (built frontend)
FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

# ---------------------------------------------------------------------------
# Logging (plain by default, JSON with LOG_FORMAT=json; secrets scrubbed)
# ---------------------------------------------------------------------------

from observability import configure_logging, request_id_var

configure_logging()
logger = logging.getLogger("api")

# ---------------------------------------------------------------------------
# Database Pool
# ---------------------------------------------------------------------------

db_pool = None
job_scheduler = None
_background_worker: Optional[job_worker.JobWorker] = None
_runtime_ready = False


def _check_auth_config():
    if AUTH_CONFIGURED:
        logger.info("Basic auth enabled (AUTH_USER=%s)", AUTH_USER)
    elif AUTH_USER or AUTH_PASSWORD:
        logger.error("AUTH MISCONFIGURED: only one of AUTH_USER / AUTH_PASSWORD is set — auth is DISABLED. Set both.")
    elif APP_ENV == "production":
        logger.warning("SECURITY WARNING: running in production (%s) with NO authentication. Set AUTH_USER and AUTH_PASSWORD.", APP_ENV)
    else:
        logger.info("Basic auth disabled — set AUTH_USER + AUTH_PASSWORD to enable")


def init_runtime() -> None:
    """Create the pool, migrate, seed snapshots and load the API key.
    Shared by the API lifespan and the standalone worker (`python worker.py`)."""
    global db_pool, _runtime_ready
    if _runtime_ready:
        return
    try:
        db_pool = pool.ThreadedConnectionPool(minconn=2, maxconn=10, **DB_CONFIG)
    except psycopg2.OperationalError as e:
        logger.error(
            "Cannot connect to database at %s:%s/%s as user '%s'. "
            "Check DATABASE_URL or DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD. Error: %s",
            DB_CONFIG["host"], DB_CONFIG["port"], DB_CONFIG["dbname"], DB_CONFIG["user"], e,
        )
        sys.exit(1)

    conn = db_pool.getconn()
    try:
        if AUTO_MIGRATE:
            applied = migrations.run_all(conn)
            logger.info("Schema migrations: %s", ", ".join(applied) if applied else "up to date")
        else:
            info = migrations.status(conn)
            if info["pending"]:
                logger.error("AUTO_MIGRATE=false and %d migration(s) pending: %s — run `python migrations.py`",
                             len(info["pending"]), ", ".join(info["pending"]))
                sys.exit(1)
        seed_snapshots(conn)
        try:
            from contact_enrichment import sync_snapshot_contacts
            contact_sync = sync_snapshot_contacts(conn)
            logger.info("Snapshot contact sync complete (%d added, %d reconciled)",
                        contact_sync["added"], contact_sync["updated"])
        except Exception:
            conn.rollback()
            logger.exception("Snapshot contact sync failed")
    finally:
        db_pool.putconn(conn)
    logger.info("Database pool initialized — connection verified")

    env_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if env_key and env_key.startswith("sk-ant-"):
        _api_key_store.update(key=env_key, valid=True, source="env")
        logger.info("Anthropic API key loaded from ANTHROPIC_API_KEY environment variable")
    _check_auth_config()
    _runtime_ready = True


def seed_snapshots(conn, snapshots_dir: Path | None = None, mode: str | None = None) -> dict:
    """Seed pitchbook_snapshots from snapshots/*_snapshot.json.

    insert_missing (default): only companies with no snapshot row get one.
    overwrite: also refresh rows that were previously seeded from a file.
    Analyst-edited rows (enriched_by = 'manual') are never overwritten.
    The file's `as_of` becomes source_as_of; imported_at records the load time.
    """
    snapshots_dir = snapshots_dir or Path(__file__).parent / "snapshots"
    mode = (mode or SNAPSHOT_SEED_MODE)
    stats = {"inserted": 0, "updated": 0, "skipped_existing": 0, "skipped_manual": 0, "no_prospect": 0, "failed": 0}
    if not snapshots_dir.is_dir():
        return stats
    snap_files = sorted(snapshots_dir.glob("*_snapshot.json"))
    if not snap_files:
        return stats
    logger.info("Seeding %d snapshot file(s) (mode=%s)...", len(snap_files), mode)
    for snap_file in snap_files:
        try:
            payload = json.loads(snap_file.read_text(encoding="utf-8"))
            snap_data = payload.get("snapshot_data", {})
            snap_md = payload.get("snapshot_markdown", "")
            ticker = (snap_data.get("ticker") or snap_file.name.split("_snapshot.json")[0]).upper()
            as_of = _parse_date(snap_data.get("as_of") or payload.get("as_of"))
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT pm.id AS prospect_id, pb.id AS snapshot_id, pb.enriched_by
                    FROM prospect_matrix pm
                    JOIN asx_listings l ON l.id = pm.listing_id
                    LEFT JOIN pitchbook_snapshots pb ON pb.prospect_id = pm.id
                    WHERE l.ticker = %s AND l.is_active = TRUE
                    LIMIT 1
                """, (ticker,))
                row = cur.fetchone()
            if not row:
                stats["no_prospect"] += 1
                logger.warning("snapshots/%s: no prospect row for %s — skipping", snap_file.name, ticker)
                continue
            with conn.cursor() as cur:
                if row["snapshot_id"] is None:
                    cur.execute("""
                        INSERT INTO pitchbook_snapshots
                            (prospect_id, snapshot_data, snapshot_markdown, enriched_at, enriched_by,
                             source_as_of, imported_at, source_file)
                        VALUES (%s, %s, %s, COALESCE(%s::timestamp, NOW()), 'seed', %s, NOW(), %s)
                    """, (str(row["prospect_id"]), Json(snap_data), snap_md, as_of, as_of, snap_file.name))
                    stats["inserted"] += 1
                elif row["enriched_by"] == "manual":
                    stats["skipped_manual"] += 1
                    continue
                elif mode == "overwrite":
                    cur.execute("""
                        UPDATE pitchbook_snapshots
                        SET snapshot_data = %s, snapshot_markdown = %s, source_as_of = %s,
                            imported_at = NOW(), source_file = %s,
                            enriched_at = COALESCE(%s::timestamp, enriched_at)
                        WHERE id = %s
                    """, (Json(snap_data), snap_md, as_of, snap_file.name, as_of, str(row["snapshot_id"])))
                    stats["updated"] += 1
                else:
                    stats["skipped_existing"] += 1
                    continue
            conn.commit()
        except Exception as e:
            stats["failed"] += 1
            conn.rollback()
            logger.error("  snapshot %s failed: %s", snap_file.name, e)
    logger.info("Snapshot seeding: %s", stats)
    return stats


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, (date, datetime)):
        return value if isinstance(value, date) else value.date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %B %Y", "%B %Y", "%Y-%m"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def worker_connect():
    """Dedicated (non-pool) connection for long-running jobs."""
    return psycopg2.connect(**DB_CONFIG)


def on_job_failure(conn, job: dict, exc: Exception) -> None:
    from alerts import notify
    notify(conn, "job_failed", f"{job['job_type']} job failed permanently: {exc}",
           payload={"job_id": str(job["id"]), "job_type": job["job_type"], "params": job.get("params")},
           severity="error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global job_scheduler, _background_worker, db_pool, _runtime_ready
    init_runtime()

    if RUN_WORKER:
        _background_worker = job_worker.JobWorker(worker_connect, on_failure=on_job_failure)
        _background_worker.start_in_thread()
        logger.info("In-process job worker started (%s)", _background_worker.worker_id)
    else:
        logger.info("RUN_WORKER=false — expecting a separate `python worker.py` process")

    if SCHEDULER_ENABLED:
        job_scheduler = BackgroundScheduler(timezone=ZoneInfo(AUTO_ASX_REFRESH_TIMEZONE))
        if AUTO_ASX_REFRESH_ENABLED:
            job_scheduler.add_job(
                _run_daily_asx_refresh_job,
                CronTrigger(hour=AUTO_ASX_REFRESH_HOUR, minute=AUTO_ASX_REFRESH_MINUTE,
                            timezone=ZoneInfo(AUTO_ASX_REFRESH_TIMEZONE)),
                id="daily_asx_refresh", replace_existing=True, max_instances=1,
            )
            logger.info("Automatic ASX refresh scheduled for %02d:%02d %s each day",
                        AUTO_ASX_REFRESH_HOUR, AUTO_ASX_REFRESH_MINUTE, AUTO_ASX_REFRESH_TIMEZONE)
        job_scheduler.add_job(_run_alert_checks, "interval", minutes=60, id="alert_checks",
                              replace_existing=True, max_instances=1)
        job_scheduler.start()

    yield

    if job_scheduler:
        job_scheduler.shutdown(wait=False)
        job_scheduler = None
        logger.info("Job scheduler stopped")
    if _background_worker:
        _background_worker.stop()
    if db_pool:
        db_pool.closeall()
        logger.info("Database pool closed")
    _reset_runtime()


def _reset_runtime() -> None:
    """Forget the pool so the next startup (tests, reload) re-initialises cleanly."""
    global db_pool, _runtime_ready
    db_pool = None
    _runtime_ready = False


def get_conn():
    return db_pool.getconn()

def put_conn(conn):
    db_pool.putconn(conn)


def _log_activity_safe(conn, prospect_id: str, activity_type: str, **kw) -> None:
    """Record a timeline entry and commit; never let logging break the caller."""
    from activity import log_activity
    try:
        log_activity(conn, prospect_id, activity_type, **kw)
        conn.commit()
    except Exception:
        conn.rollback()
        logger.debug("activity log failed for %s/%s", prospect_id, activity_type, exc_info=True)

# ---------------------------------------------------------------------------
# App Setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Delta Prospect System API",
    version="2.1.0",
    description="Intelligence-driven prospect management for ASX-listed companies",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    """Attach a request id to every request/response and emit one access-log line."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    token = request_id_var.set(request_id)
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("unhandled error %s %s", request.method, request.url.path)
        response = JSONResponse(status_code=500, content={"detail": "Internal server error", "request_id": request_id})
    duration_ms = int((time.monotonic() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    if request.url.path.startswith("/api/"):
        logger.info("%s %s -> %s %dms", request.method, request.url.path, response.status_code, duration_ms)
    request_id_var.reset(token)
    return response


# ---------------------------------------------------------------------------
# Basic Auth (optional — only active when AUTH_USER + AUTH_PASSWORD are set)
# ---------------------------------------------------------------------------

import base64
import secrets

PUBLIC_PATHS = {"/api/health", "/api/health/live", "/api/health/ready"}

def check_auth(request: Request):
    """Verify Basic Auth if AUTH_USER and AUTH_PASSWORD are configured."""
    if not AUTH_CONFIGURED:
        return  # Auth not configured — allow all
    if request.url.path in PUBLIC_PATHS:
        return  # Health checks are always public

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            user, pw = decoded.split(":", 1)
            if secrets.compare_digest(user, AUTH_USER) and secrets.compare_digest(pw, AUTH_PASSWORD):
                return
        except Exception:
            pass

    raise HTTPException(
        status_code=401,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Basic realm=\"Delta Prospect System\""},
    )

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # AUTH_CONFIGURED is read per request so tests (and future hot-reload of
    # credentials) can toggle it without re-importing the module.
    if not AUTH_CONFIGURED or request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    try:
        check_auth(request)
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )
    return await call_next(request)

# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

OPPORTUNITY_STAGES = ("none", "researching", "outreach_planned", "contacted", "meeting", "proposal", "won", "lost")


class ProspectUpdate(BaseModel):
    status: Optional[str] = None
    analyst_notes: Optional[str] = None
    dq_reason: Optional[str] = None
    primary_buyer_name: Optional[str] = None
    primary_buyer_role: Optional[str] = None
    primary_buyer_linkedin: Optional[str] = None
    secondary_buyer_name: Optional[str] = None
    secondary_buyer_role: Optional[str] = None
    secondary_buyer_linkedin: Optional[str] = None
    network_path: Optional[str] = None
    warm_intro_contact: Optional[str] = None
    # Phase 4 workflow
    owner: Optional[str] = None
    next_action: Optional[str] = None
    next_action_due: Optional[str] = None      # ISO date; "" clears
    last_contacted_at: Optional[str] = None    # ISO date; "" clears
    opportunity_stage: Optional[str] = None
    outcome_reason: Optional[str] = None
    # Optimistic concurrency: send the row_version you loaded; 409 if it moved.
    expected_version: Optional[int] = None
    actor: str = "api"


class SnapshotUpsert(BaseModel):
    snapshot_data: dict = Field(default_factory=dict)
    snapshot_markdown: Optional[str] = None


class RefreshRequest(BaseModel):
    triggered_by: str = "manual"


class ApiKeyRequest(BaseModel):
    api_key: str


class V3CollectRequest(BaseModel):
    max_documents: int = Field(default=6, ge=1, le=20)
    force_refresh: bool = False


class ContactBatchRequest(BaseModel):
    force: bool = False

# ---------------------------------------------------------------------------
# Endpoints: Prospects
# ---------------------------------------------------------------------------

@app.get("/api/prospects")
def list_prospects(
    status: Optional[str] = Query(None),
    sector: Optional[str] = Query(None),
    industry: Optional[str] = Query(None),
    lead_tier: Optional[str] = Query(None),
    min_score: Optional[float] = Query(None),
    min_prize: Optional[int] = Query(None, description="Min size_of_prize in dollars"),
    min_strong_signals: Optional[int] = Query(None),
    has_signals: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    watchlist: Optional[bool] = Query(None),
    australia_only: bool = Query(False),
    city: Optional[str] = Query(None, description="Comma-separated city names to filter"),
    exclude_dq: bool = Query(False, description="Exclude disqualified and archived prospects"),
    sort_by: str = Query("prospect_score"),
    sort_dir: str = Query("desc"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List prospects with filtering, sorting, and pagination."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            where_clauses = ["l.is_active = TRUE", "l.is_target_sector = TRUE"]
            where_params = []

            if status:
                where_clauses.append("pm.status = %s")
                where_params.append(status)
            if sector:
                where_clauses.append("l.gics_sector = %s")
                where_params.append(sector)
            if industry:
                where_clauses.append("l.gics_industry_group = %s")
                where_params.append(industry)
            if lead_tier:
                where_clauses.append("pm.lead_tier = %s")
                where_params.append(lead_tier)
            if min_score is not None:
                where_clauses.append("pm.prospect_score >= %s")
                where_params.append(min_score)
            if min_prize is not None:
                where_clauses.append("pm.size_of_prize >= %s")
                where_params.append(min_prize)
            if australia_only:
                where_clauses.append("pm.in_australia = TRUE")
            if city:
                cities = [c.strip() for c in city.split(",") if c.strip()]
                if cities:
                    placeholders = ",".join(["%s"] * len(cities))
                    where_clauses.append(f"pm.registered_city IN ({placeholders})")
                    where_params.extend(cities)
            if search:
                where_clauses.append("(l.company_name ILIKE %s OR l.ticker ILIKE %s)")
                where_params.extend([f"%{search}%", f"%{search}%"])
            if watchlist:
                where_clauses.append("pm.is_watchlisted = TRUE")
            if exclude_dq:
                where_clauses.append("pm.status NOT IN ('disqualified', 'archived')")

            having_clauses = []
            having_params = []
            if min_strong_signals is not None and min_strong_signals > 0:
                having_clauses.append(
                    "COUNT(ps.id) FILTER (WHERE ps.strength = 'strong') >= %s"
                )
                having_params.append(min_strong_signals)
            if has_signals:
                having_clauses.append("COUNT(ps.id) > 0")

            where_sql = " AND ".join(where_clauses)
            having_sql = f"HAVING {' AND '.join(having_clauses)}" if having_clauses else ""

            valid_sorts = {
                "prospect_score", "company_name", "ticker", "market_cap_aud",
                "status", "updated_at", "total_signals", "likelihood_score",
                "lead_tier", "size_of_prize", "has_snapshot", "contact_count",
            }
            if sort_by not in valid_sorts:
                sort_by = "prospect_score"
            sort_direction = "DESC" if sort_dir.lower() == "desc" else "ASC"
            nulls = "NULLS LAST" if sort_direction == "DESC" else "NULLS FIRST"

            query = f"""
                SELECT
                    pm.id AS prospect_id,
                    l.ticker,
                    l.company_name,
                    l.gics_sector,
                    l.gics_industry_group,
                    l.market_cap_aud,
                    l.last_price_aud,
                    l.website,
                    pm.status,
                    pm.is_watchlisted,
                    pm.prospect_score,
                    pm.lead_tier,
                    pm.strategic_direction,
                    pm.primary_tailwind,
                    pm.primary_headwind,
                    pm.likelihood_score,
                    pm.primary_buyer_name,
                    pm.primary_buyer_role,
                    pm.network_path,
                    pm.analyst_notes,
                    pm.updated_at,
                    pm.registered_city,
                    pm.registered_state,
                    pm.in_australia,
                    pm.size_of_prize,
                    (SELECT COUNT(*) FROM prospect_contacts pc
                     WHERE pc.prospect_id = pm.id AND pc.is_active = TRUE) AS contact_count,
                    (SELECT COUNT(*) FROM prospect_contacts pc
                     WHERE pc.prospect_id = pm.id AND pc.is_active = TRUE
                       AND pc.email IS NOT NULL
                       AND COALESCE(pc.email_status, '') NOT IN ('invalid', 'disposable')) AS verified_contact_count,
                    COUNT(ps.id) AS total_signals,
                    COUNT(ps.id) FILTER (WHERE ps.strength = 'strong') AS strong_signals,
                    COUNT(ps.id) FILTER (WHERE ps.pressure_type::text = 'production') AS sig_production,
                    COUNT(ps.id) FILTER (WHERE ps.pressure_type::text = 'license_to_operate') AS sig_license,
                    COUNT(ps.id) FILTER (WHERE ps.pressure_type::text = 'cost') AS sig_cost,
                    COUNT(ps.id) FILTER (WHERE ps.pressure_type::text = 'people') AS sig_people,
                    COUNT(ps.id) FILTER (WHERE ps.pressure_type::text = 'quality') AS sig_quality,
                    COUNT(ps.id) FILTER (WHERE ps.pressure_type::text = 'future_readiness') AS sig_future,
                    (SELECT ps2.summary FROM pressure_signals ps2
                     WHERE ps2.prospect_id = pm.id
                     ORDER BY CASE ps2.strength WHEN 'strong' THEN 1 WHEN 'moderate' THEN 2 ELSE 3 END,
                              ps2.detected_at DESC LIMIT 1) AS top_signal,
                    (SELECT ps2.source_url FROM pressure_signals ps2
                     WHERE ps2.prospect_id = pm.id
                     ORDER BY CASE ps2.strength WHEN 'strong' THEN 1 WHEN 'moderate' THEN 2 ELSE 3 END,
                              ps2.detected_at DESC LIMIT 1) AS top_signal_url,
                    EXISTS(SELECT 1 FROM pitchbook_snapshots pb WHERE pb.prospect_id = pm.id) AS has_snapshot
                FROM prospect_matrix pm
                JOIN asx_listings l ON l.id = pm.listing_id
                LEFT JOIN pressure_signals ps ON ps.prospect_id = pm.id
                WHERE {where_sql}
                GROUP BY pm.id, l.id
                {having_sql}
                ORDER BY {sort_by} {sort_direction} {nulls}
                LIMIT %s OFFSET %s
            """
            cur.execute(query, where_params + having_params + [limit, offset])
            prospects = cur.fetchall()

            # Count — use subquery when HAVING is active (can't use COUNT DISTINCT across aggregates)
            if having_clauses:
                count_query = f"""
                    SELECT COUNT(*) AS count FROM (
                        SELECT pm.id
                        FROM prospect_matrix pm
                        JOIN asx_listings l ON l.id = pm.listing_id
                        LEFT JOIN pressure_signals ps ON ps.prospect_id = pm.id
                        WHERE {where_sql}
                        GROUP BY pm.id, l.id
                        {having_sql}
                    ) sub
                """
                cur.execute(count_query, where_params + having_params)
            else:
                count_query = f"""
                    SELECT COUNT(DISTINCT pm.id) AS count
                    FROM prospect_matrix pm
                    JOIN asx_listings l ON l.id = pm.listing_id
                    WHERE {where_sql}
                """
                cur.execute(count_query, where_params)
            total = cur.fetchone()["count"]

            return {"data": prospects, "total": total, "limit": limit, "offset": offset}
    finally:
        put_conn(conn)


@app.get("/api/prospects/export/csv")
def export_prospects_csv(
    status: Optional[str] = Query(None),
    sector: Optional[str] = Query(None),
    industry: Optional[str] = Query(None),
    lead_tier: Optional[str] = Query(None),
    min_score: Optional[float] = Query(None),
    min_strong_signals: Optional[int] = Query(None),
    has_signals: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    watchlist: Optional[bool] = Query(None),
    full: bool = Query(False),
    min_prize: Optional[int] = Query(None),
    australia_only: bool = Query(False),
    city: Optional[str] = Query(None),
):
    """Export all matching prospects as a CSV download (no pagination)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            where_clauses = ["l.is_active = TRUE", "l.is_target_sector = TRUE"]
            where_params = []
            if status:
                where_clauses.append("pm.status = %s")
                where_params.append(status)
            if sector:
                where_clauses.append("l.gics_sector = %s")
                where_params.append(sector)
            if industry:
                where_clauses.append("l.gics_industry_group = %s")
                where_params.append(industry)
            if lead_tier:
                where_clauses.append("pm.lead_tier = %s")
                where_params.append(lead_tier)
            if min_score is not None:
                where_clauses.append("pm.prospect_score >= %s")
                where_params.append(min_score)
            if search:
                where_clauses.append("(l.company_name ILIKE %s OR l.ticker ILIKE %s)")
                where_params.extend([f"%{search}%", f"%{search}%"])
            if watchlist:
                where_clauses.append("pm.is_watchlisted = TRUE")
            if min_prize is not None:
                where_clauses.append("pm.size_of_prize >= %s")
                where_params.append(min_prize)
            if australia_only:
                where_clauses.append("pm.in_australia = TRUE")
            if city:
                cities = [value.strip() for value in city.split(',') if value.strip()]
                if cities:
                    where_clauses.append("pm.registered_city = ANY(%s)")
                    where_params.append(cities)

            having_clauses = []
            having_params = []
            if min_strong_signals is not None and min_strong_signals > 0:
                having_clauses.append(
                    "COUNT(ps.id) FILTER (WHERE ps.strength = 'strong') >= %s"
                )
                having_params.append(min_strong_signals)
            if has_signals:
                having_clauses.append("COUNT(ps.id) > 0")

            where_sql = " AND ".join(where_clauses)
            having_sql = f"HAVING {' AND '.join(having_clauses)}" if having_clauses else ""

            # Subquery for per-pillar top signal text
            pillar_top_sql = """
                (SELECT ps2.summary FROM pressure_signals ps2
                 WHERE ps2.prospect_id = pm.id AND ps2.pressure_type::text = '{pt}'
                 ORDER BY CASE ps2.strength WHEN 'strong' THEN 1 WHEN 'moderate' THEN 2 ELSE 3 END
                 LIMIT 1) AS top_{alias}"""

            cur.execute(f"""
                SELECT
                    pm.id AS prospect_id,
                    l.ticker,
                    l.company_name,
                    l.gics_sector,
                    l.gics_industry_group,
                    l.market_cap_aud,
                    pm.lead_tier,
                    pm.status,
                    pm.prospect_score,
                    COUNT(ps.id) AS total_signals,
                    COUNT(ps.id) FILTER (WHERE ps.pressure_type::text = 'production') AS sig_production,
                    COUNT(ps.id) FILTER (WHERE ps.pressure_type::text = 'license_to_operate') AS sig_license,
                    COUNT(ps.id) FILTER (WHERE ps.pressure_type::text = 'cost') AS sig_cost,
                    COUNT(ps.id) FILTER (WHERE ps.pressure_type::text = 'people') AS sig_people,
                    COUNT(ps.id) FILTER (WHERE ps.pressure_type::text = 'quality') AS sig_quality,
                    COUNT(ps.id) FILTER (WHERE ps.pressure_type::text = 'future_readiness') AS sig_future,
                    (SELECT ps2.summary FROM pressure_signals ps2
                     WHERE ps2.prospect_id = pm.id
                     ORDER BY CASE ps2.strength WHEN 'strong' THEN 1 WHEN 'moderate' THEN 2 ELSE 3 END,
                              ps2.detected_at DESC LIMIT 1) AS top_signal,
                    (SELECT ps2.source_title FROM pressure_signals ps2
                     WHERE ps2.prospect_id = pm.id
                     ORDER BY CASE ps2.strength WHEN 'strong' THEN 1 WHEN 'moderate' THEN 2 ELSE 3 END,
                              ps2.detected_at DESC LIMIT 1) AS top_signal_source,
                    (SELECT ps2.source_url FROM pressure_signals ps2
                     WHERE ps2.prospect_id = pm.id
                     ORDER BY CASE ps2.strength WHEN 'strong' THEN 1 WHEN 'moderate' THEN 2 ELSE 3 END,
                              ps2.detected_at DESC LIMIT 1) AS top_signal_url,
                    MAX(ps.source_date) AS latest_signal_date,
                    pm.analyst_notes,
                    pm.registered_city,
                    pm.registered_state,
                    pm.size_of_prize
                FROM prospect_matrix pm
                JOIN asx_listings l ON l.id = pm.listing_id
                LEFT JOIN pressure_signals ps ON ps.prospect_id = pm.id
                WHERE {where_sql}
                GROUP BY pm.id, l.id
                {having_sql}
                ORDER BY
                    CASE pm.lead_tier WHEN 'hot' THEN 1 WHEN 'warm' THEN 2 WHEN 'watch' THEN 3 ELSE 4 END,
                    pm.prospect_score DESC NULLS LAST
            """, where_params + having_params)
            rows = cur.fetchall()

        if full:
            from company_report import display_value, report_rows

            output = io.StringIO(newline='')
            reports = [_company_report_data(conn, str(row['prospect_id'])) for row in rows]
            records = []
            for report in reports:
                record = {'Ticker': report['prospect']['ticker'], 'Company Name': report['prospect']['company_name']}
                for section, field, value in report_rows(report)[1:]:
                    record[f'{section}: {field}'] = value
                # Preserve stored fields beyond the human-readable report template.
                for section, value in report.items():
                    record[f'Complete {section} data'] = display_value(value)
                records.append(record)
            columns = ['Ticker', 'Company Name']
            for record in records:
                columns.extend(key for key in record if key not in columns)
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            writer.writerows(records)
            return StreamingResponse(
                iter([output.getvalue().encode('utf-8-sig')]), media_type='text/csv',
                headers={'Content-Disposition': 'attachment; filename="all_company_profiles_and_contacts.csv"'},
            )

        def fmt_cap(cents):
            if not cents: return ""
            aud = cents / 100
            if aud >= 1e9: return f"${aud/1e9:.1f}B"
            if aud >= 1e6: return f"${aud/1e6:.0f}M"
            return f"${aud/1e3:.0f}K"

        def deal_fit(prize):
            if not prize: return ""
            if prize >= 50_000_000: return "ENTERPRISE"
            if prize >= 5_000_000: return "SWEET SPOT"
            return "SMALL"

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Ticker", "Company Name", "Sector", "Industry Group", "Market Cap",
            "Lead Tier", "Production", "License to Operate", "Cost", "People",
            "Quality", "Future Readiness", "Total Signals", "Top Signal",
            "Source Announcement", "Source URL", "Latest Signal Date",
            "Status", "Prospect Score", "City", "State",
            "Est. Impact ($)", "Deal Fit", "Analyst Notes",
        ])
        for row in rows:
            writer.writerow([
                row["ticker"],
                row["company_name"],
                row["gics_sector"] or "",
                row["gics_industry_group"] or "",
                fmt_cap(row["market_cap_aud"]),
                (row["lead_tier"] or "not_qualified").replace("_", " ").title(),
                row["sig_production"],
                row["sig_license"],
                row["sig_cost"],
                row["sig_people"],
                row["sig_quality"],
                row["sig_future"],
                row["total_signals"],
                row["top_signal"] or "",
                row["top_signal_source"] or "",
                row["top_signal_url"] or "",
                row["latest_signal_date"] or "",
                row["status"],
                float(row["prospect_score"]) if row["prospect_score"] else "",
                row["registered_city"] or "",
                row["registered_state"] or "",
                row["size_of_prize"] or "",
                deal_fit(row["size_of_prize"]),
                (row["analyst_notes"] or "").replace("\n", " "),
            ])

        today = datetime.now().strftime("%Y%m%d")
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="delta_leads_{today}.csv"'},
        )
    finally:
        put_conn(conn)


def _contact_prospects_needing_contacts(conn, force: bool = False) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT pm.id AS prospect_id, l.ticker, l.company_name, l.website,
                   pm.prospect_score, pm.lead_tier, pm.size_of_prize,
                   COUNT(pc.id) FILTER (WHERE pc.is_active = TRUE) AS active_contact_count
            FROM prospect_matrix pm
            JOIN asx_listings l ON l.id = pm.listing_id
            LEFT JOIN prospect_contacts pc ON pc.prospect_id = pm.id
            WHERE l.is_active = TRUE
              AND l.is_target_sector = TRUE
              AND pm.status NOT IN ('disqualified', 'archived')
              AND (
                    %s = TRUE
                    OR pm.contact_enrichment_attempted_at IS NULL
                    OR pm.contact_enrichment_attempted_at < NOW() - INTERVAL '30 days'
              )
            GROUP BY pm.id, l.id
            HAVING COUNT(pc.id) FILTER (WHERE pc.is_active = TRUE) < 2
            ORDER BY pm.prospect_score DESC NULLS LAST,
                     pm.size_of_prize DESC NULLS LAST,
                     l.market_cap_aud DESC NULLS LAST
        """, (force,))
        return list(cur.fetchall())


def _contact_rows(conn, prospect_id: str) -> list[dict]:
    from contact_enrichment import classify_contact
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT id, full_name, first_name, last_name, title, seniority,
                   departments, decision_rank, email, email_status, phone,
                   linkedin_url, source_provider, confidence_score,
                   discovered_at, last_verified_at, updated_at, raw_data
            FROM prospect_contacts
            WHERE prospect_id = %s AND is_active = TRUE
            ORDER BY decision_rank DESC, confidence_score DESC NULLS LAST,
                     full_name ASC
        """, (prospect_id,))
        rows = []
        for row in cur.fetchall():
            row = dict(row)
            row["contact_kind"] = classify_contact(row)
            row.pop("raw_data", None)
            rows.append(row)
        return rows


CONTACT_PROGRESS_DEFAULT = {
    "running": False, "current": 0, "total": 0, "ticker": "",
    "companies_ok": 0, "contacts_added": 0, "contacts_updated": 0,
    "failures": 0, "providers_unavailable": [], "message": "",
}


def _job_contact_batch(conn, job: dict, progress):
    """Worker handler: find contacts for every company that still needs them.
    Resumable — tickers already processed are kept in progress['done']."""
    from contact_enrichment import enrich_company_contacts

    force = bool((job.get("params") or {}).get("force"))
    done = set(progress.get("done") or [])
    # Keep counters from an interrupted attempt so the totals stay honest on resume.
    carried = {k: progress.get(k, CONTACT_PROGRESS_DEFAULT[k])
               for k in ("companies_ok", "contacts_added", "contacts_updated", "failures", "providers_unavailable")}
    progress.update({**CONTACT_PROGRESS_DEFAULT, **carried, "running": True, "done": sorted(done),
                     "message": "Loading companies needing contacts"})
    prospects = _contact_prospects_needing_contacts(conn, force=force)
    progress["total"] = len(prospects)
    apollo_available = "apollo" not in progress["providers_unavailable"]
    hunter_available = "hunter" not in progress["providers_unavailable"]
    for index, prospect in enumerate(prospects, start=1):
        ticker = prospect["ticker"]
        if ticker in done:
            progress["current"] = index
            continue
        progress.update({"current": index - 1, "ticker": ticker, "message": f"Finding contacts for {ticker}"})
        result = enrich_company_contacts(
            conn, prospect, force=force, allow_apollo_second=True,
            allow_apollo=apollo_available, allow_hunter=hunter_available,
        )
        unavailable = set(result.get("provider_unavailable") or [])
        apollo_available = apollo_available and "apollo" not in unavailable
        hunter_available = hunter_available and "hunter" not in unavailable
        progress.data["providers_unavailable"] = sorted(set(progress["providers_unavailable"]) | unavailable)
        progress.data["contacts_added"] += result["added"]
        progress.data["contacts_updated"] += result["updated"]
        if result["added"] or result["updated"]:
            progress.data["companies_ok"] += 1
        if result.get("contact_count", 0) == 0:
            progress.data["failures"] += 1
        done.add(ticker)
        progress.data["done"] = sorted(done)
        progress.data["current"] = index
        progress.checkpoint()
    progress.update({"running": False, "ticker": "", "message": "Contact enrichment complete"})
    return {"companies": len(prospects), "contacts_added": progress["contacts_added"],
            "contacts_updated": progress["contacts_updated"], "failures": progress["failures"]}


job_worker.register("contact_batch", _job_contact_batch)


def _enqueue_contact_batch(conn, force: bool = False, triggered_by: str = "api") -> dict:
    return jobs.enqueue(conn, "contact_batch", {"force": force}, idempotency_key="contact_batch",
                        triggered_by=triggered_by, priority=7)


def _job_status_payload(job: dict | None, defaults: dict) -> dict:
    """Merge a job row's progress into the legacy status shape the dashboard polls."""
    payload = dict(defaults)
    if not job:
        return payload
    payload.update(job.get("progress") or {})
    payload["running"] = job["status"] in ("queued", "running")
    payload["job_id"] = str(job["id"])
    payload["job_status"] = job["status"]
    if job.get("error"):
        payload["error"] = job["error"]
    for key in ("created_at", "started_at", "finished_at", "updated_at"):
        value = job.get(key)
        payload[key] = value.isoformat() if hasattr(value, "isoformat") else value
    payload.pop("done", None)
    return payload


@app.get("/api/contacts/status")
def get_contact_enrichment_status():
    conn = get_conn()
    try:
        return _job_status_payload(jobs.latest_job(conn, "contact_batch"), CONTACT_PROGRESS_DEFAULT)
    finally:
        put_conn(conn)


@app.get("/api/contacts/providers")
def get_contact_provider_status():
    conn = get_conn()
    try:
        latest = jobs.latest_job(conn, "contact_batch") or {}
    finally:
        put_conn(conn)
    return {
        "apollo": bool(os.getenv("APOLLO_API_KEY", "").strip()),
        "hunter": bool(os.getenv("HUNTER_API_KEY", "").strip()),
        "providers_unavailable_this_run": (latest.get("progress") or {}).get("providers_unavailable") or [],
        "public_website_fallback": True,
    }


@app.post("/api/contacts/enrich-all")
@app.post("/api/contacts/enrich-top", include_in_schema=False)
def enrich_all_contacts(req: ContactBatchRequest):
    conn = get_conn()
    try:
        job = _enqueue_contact_batch(conn, force=req.force)
    finally:
        put_conn(conn)
    if job.get("existing"):
        return {"message": "Contact enrichment is already running", "job_id": str(job["id"]), "running": True,
                **_job_status_payload(job, CONTACT_PROGRESS_DEFAULT)}
    if not os.getenv("APOLLO_API_KEY", "").strip() and not os.getenv("HUNTER_API_KEY", "").strip():
        logger.info("Contact enrichment running with public website and snapshot sources only")
    return {"message": "Contact enrichment queued for every company needing contacts", "job_id": str(job["id"])}


@app.post("/api/prospects/{prospect_id}/contacts/enrich")
def enrich_prospect_contacts(prospect_id: str, force: bool = Query(False)):
    from contact_enrichment import enrich_company_contacts

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT pm.id AS prospect_id, l.ticker, l.company_name, l.website,
                       pm.prospect_score, pm.lead_tier, pm.size_of_prize
                FROM prospect_matrix pm
                JOIN asx_listings l ON l.id = pm.listing_id
                WHERE pm.id = %s
            """, (prospect_id,))
            prospect = cur.fetchone()
        if not prospect:
            raise HTTPException(status_code=404, detail="Prospect not found")
        result = enrich_company_contacts(conn, prospect, force=force, allow_apollo_second=True)
        result["contacts"] = _contact_rows(conn, prospect_id)
        return result
    finally:
        put_conn(conn)


@app.get("/api/prospects/{prospect_id}/contacts")
def get_prospect_contacts(prospect_id: str):
    conn = get_conn()
    try:
        return {"contacts": _contact_rows(conn, prospect_id)}
    finally:
        put_conn(conn)


def _csv_cell(value):
    if value is None:
        return ""
    text = str(value)
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _write_contacts_csv(rows: list[dict]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Ticker", "Company", "Rank", "Name", "Title", "Seniority",
        "Departments", "Email", "Email Status", "Phone", "LinkedIn",
        "Source", "Confidence", "Discovered", "Last Verified",
    ])
    for row in rows:
        writer.writerow([_csv_cell(value) for value in (
            row.get("ticker"), row.get("company_name"), row.get("decision_rank"),
            row.get("full_name"), row.get("title"), row.get("seniority"),
            "; ".join(row.get("departments") or []), row.get("email"),
            row.get("email_status"), row.get("phone"), row.get("linkedin_url"),
            row.get("source_provider"), row.get("confidence_score"),
            row.get("discovered_at"), row.get("last_verified_at"),
        )])
    return output.getvalue()


@app.get("/api/prospects/{prospect_id}/contacts/csv")
def export_prospect_contacts_csv(prospect_id: str):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT l.ticker, l.company_name, pc.*
                FROM prospect_contacts pc
                JOIN prospect_matrix pm ON pm.id = pc.prospect_id
                JOIN asx_listings l ON l.id = pm.listing_id
                WHERE pc.prospect_id = %s AND pc.is_active = TRUE
                ORDER BY pc.decision_rank DESC, pc.confidence_score DESC NULLS LAST
            """, (prospect_id,))
            rows = list(cur.fetchall())
        if not rows:
            raise HTTPException(status_code=404, detail="No contacts found for this prospect")
        ticker = rows[0]["ticker"]
        return StreamingResponse(
            iter([_write_contacts_csv(rows)]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{ticker}_contacts.csv"'},
        )
    finally:
        put_conn(conn)


@app.get("/api/contacts/export/csv")
def export_all_contacts_csv():
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT l.ticker, l.company_name, pc.*
                FROM prospect_matrix pm
                JOIN asx_listings l ON l.id = pm.listing_id
                JOIN prospect_contacts pc ON pc.prospect_id = pm.id
                WHERE l.is_active = TRUE AND l.is_target_sector = TRUE
                  AND pm.status NOT IN ('disqualified', 'archived')
                  AND pc.is_active = TRUE
                ORDER BY l.ticker, pc.decision_rank DESC, pc.confidence_score DESC NULLS LAST
            """)
            rows = list(cur.fetchall())
        today = datetime.now().strftime("%Y%m%d")
        return StreamingResponse(
            iter([_write_contacts_csv(rows)]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="all_contacts_{today}.csv"'},
        )
    finally:
        put_conn(conn)


def _company_report_data(conn, prospect_id: str) -> dict:
    """Collect all stored intelligence for a portable company report."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT pm.*, l.ticker, l.company_name, l.gics_sector, l.gics_industry_group,
                   l.market_cap_aud, l.last_price_aud, l.website,
                   l.principal_activities, l.listing_date
            FROM prospect_matrix pm
            JOIN asx_listings l ON l.id = pm.listing_id
            WHERE pm.id = %s
        """, (prospect_id,))
        prospect = cur.fetchone()
        if not prospect:
            raise HTTPException(status_code=404, detail="Prospect not found")

        cur.execute("""
            SELECT * FROM pressure_signals
            WHERE prospect_id = %s
            ORDER BY CASE strength WHEN 'strong' THEN 1 WHEN 'moderate' THEN 2 WHEN 'weak' THEN 3 END,
                     detected_at DESC
        """, (prospect_id,))
        signals = list(cur.fetchall())

        cur.execute("""
            SELECT snapshot_markdown, snapshot_data, enriched_at
            FROM pitchbook_snapshots
            WHERE prospect_id = %s
            ORDER BY enriched_at DESC NULLS LAST
            LIMIT 1
        """, (prospect_id,))
        snapshot = cur.fetchone()

        cur.execute("""
            SELECT analysis_type, provider, model_name, source_count, summary,
                   output_json, completed_at
            FROM prospect_intelligence_runs
            WHERE prospect_id = %s
            ORDER BY completed_at DESC NULLS LAST, created_at DESC
            LIMIT 1
        """, (prospect_id,))
        v3_analysis = cur.fetchone()

        cur.execute("""
            SELECT MAX(completed_at) AS last_enriched_at FROM enrichment_log
            WHERE listing_id = %s AND success
        """, (prospect["listing_id"],))
        research = cur.fetchone() or {}
        cur.execute("""
            SELECT activity_type, actor, field, old_value, new_value, note, created_at
            FROM prospect_activity WHERE prospect_id = %s ORDER BY created_at DESC LIMIT 25
        """, (prospect_id,))
        activity = list(cur.fetchall())

    return {
        "prospect": prospect,
        "signals": signals,
        "contacts": _contact_rows(conn, prospect_id),
        "snapshot": snapshot,
        "v3_analysis": v3_analysis,
        "research": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "last_enriched_at": research.get("last_enriched_at"),
            "score_policy_version": prospect.get("score_policy_version"),
            "score_breakdown": prospect.get("score_breakdown"),
        },
        "activity": activity,
    }


@app.get("/api/prospects/{prospect_id}/report")
def export_company_report(
    prospect_id: str,
    report_format: str = Query("pdf", alias="format"),
):
    """Download all stored company intelligence as a PDF, Word document, or CSV."""
    format_key = report_format.lower().strip()
    exporters = {
        "pdf": ("application/pdf", "pdf", "build_pdf"),
        "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx", "build_docx"),
        "csv": ("text/csv", "csv", "build_csv"),
    }
    if format_key not in exporters:
        raise HTTPException(status_code=400, detail="format must be pdf, docx, or csv")

    conn = get_conn()
    try:
        report = _company_report_data(conn, prospect_id)
        _log_activity_safe(conn, prospect_id, "report_export", actor="api", note=f"{format_key} export")
    finally:
        put_conn(conn)

    try:
        from company_report import build_csv, build_docx, build_pdf, safe_filename

        builders = {"build_pdf": build_pdf, "build_docx": build_docx, "build_csv": build_csv}
        media_type, extension, builder_name = exporters[format_key]
        payload = builders[builder_name](report)
        filename = f"{safe_filename(report['prospect'].get('ticker') or report['prospect'].get('company_name'))}_company_report.{extension}"
        return StreamingResponse(
            iter([payload]),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Company report export failed for %s", prospect_id)
        raise HTTPException(status_code=500, detail=f"Company report export failed: {exc}") from exc


@app.get("/api/prospects/{prospect_id}")
def get_prospect(prospect_id: str):
    """Get a single prospect with all its pressure signals."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get prospect
            cur.execute("""
                SELECT
                    pm.*,
                    l.ticker, l.company_name, l.gics_sector, l.gics_industry_group,
                    l.market_cap_aud, l.last_price_aud, l.website,
                    l.principal_activities, l.listing_date
                FROM prospect_matrix pm
                JOIN asx_listings l ON l.id = pm.listing_id
                WHERE pm.id = %s
            """, (prospect_id,))
            prospect = cur.fetchone()
            
            if not prospect:
                raise HTTPException(status_code=404, detail="Prospect not found")
            
            # Get pressure signals
            cur.execute("""
                SELECT *
                FROM pressure_signals
                WHERE prospect_id = %s
                ORDER BY 
                    CASE strength
                        WHEN 'strong' THEN 1
                        WHEN 'moderate' THEN 2
                        WHEN 'weak' THEN 3
                    END,
                    detected_at DESC
            """, (prospect_id,))
            signals = cur.fetchall()
            
            # Get enrichment history
            cur.execute("""
                SELECT *
                FROM enrichment_log
                WHERE listing_id = %s
                ORDER BY started_at DESC
                LIMIT 10
            """, (prospect["listing_id"],))
            enrichment_history = cur.fetchall()

            # Last deep analysis timestamp (derived from enrichment_log)
            cur.execute("""
                SELECT completed_at FROM enrichment_log
                WHERE listing_id = %s
                  AND agent_version IN ('claude-deep-v1', 'claude-deep-v2-firecrawl')
                ORDER BY completed_at DESC LIMIT 1
            """, (prospect["listing_id"],))
            da_row = cur.fetchone()
            last_deep_analysis_at = da_row["completed_at"] if da_row else None

            cur.execute("""
                SELECT id, source_url, source_title, source_date, document_type,
                       provider, fetch_status, fetched_at, last_error
                FROM announcement_documents
                WHERE listing_id = %s
                ORDER BY source_date DESC NULLS LAST, fetched_at DESC NULLS LAST
                LIMIT 10
            """, (prospect["listing_id"],))
            v3_documents = cur.fetchall()

            cur.execute("""
                SELECT analysis_type, provider, model_name, source_count, summary,
                       output_json, completed_at
                FROM prospect_intelligence_runs
                WHERE prospect_id = %s
                ORDER BY completed_at DESC NULLS LAST, created_at DESC
                LIMIT 1
            """, (prospect_id,))
            latest_v3_analysis = cur.fetchone()

            contacts = _contact_rows(conn, prospect_id)
            from activity import recent_activity
            activity = recent_activity(conn, prospect_id, 50)
            for sig in signals:
                ev = sig.get("source_date") or (sig.get("detected_at").date() if sig.get("detected_at") else None)
                sig["evidence_age_days"] = (date.today() - ev).days if ev else None

            try:
                from v3_intelligence import firecrawl_is_configured
                firecrawl_available = firecrawl_is_configured()
            except Exception:
                firecrawl_available = False

            return {
                "prospect": prospect,
                "signals": signals,
                "enrichment_history": enrichment_history,
                "deep_analysis_available": _api_key_store["valid"],
                "last_deep_analysis_at": last_deep_analysis_at,
                "firecrawl_available": firecrawl_available,
                "v3_documents": v3_documents,
                "v3_latest_analysis": latest_v3_analysis,
                "contacts": contacts,
                "activity": activity,
            }
    finally:
        put_conn(conn)


@app.patch("/api/prospects/{prospect_id}")
def update_prospect(prospect_id: str, update: ProspectUpdate):
    """Update a prospect's status, notes, buyer info or workflow fields.
    Every changed field is written to the activity timeline. Pass
    expected_version to detect concurrent edits (409 on mismatch)."""
    from activity import log_activity

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            update_dict = update.model_dump(exclude_none=True)
            actor = update_dict.pop("actor", "api")
            expected_version = update_dict.pop("expected_version", None)
            if not update_dict:
                raise HTTPException(status_code=400, detail="No fields to update")

            valid_statuses = {
                "unscreened", "qualified", "enriched", "ready_for_outreach",
                "suggested_dq", "disqualified", "archived"
            }
            if update.status and update.status not in valid_statuses:
                raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {sorted(valid_statuses)}")
            if update.opportunity_stage and update.opportunity_stage not in OPPORTUNITY_STAGES:
                raise HTTPException(status_code=400, detail=f"Invalid opportunity_stage. Must be one of: {list(OPPORTUNITY_STAGES)}")

            cur.execute(f"SELECT row_version, {', '.join(update_dict)} FROM prospect_matrix WHERE id = %s FOR UPDATE", (prospect_id,))
            before = cur.fetchone()
            if not before:
                raise HTTPException(status_code=404, detail="Prospect not found")
            if expected_version is not None and before["row_version"] != expected_version:
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={"message": "This prospect was changed by someone else since you loaded it. Reload and try again.",
                            "current_version": before["row_version"], "expected_version": expected_version},
                )

            set_clauses, params = [], []
            for field_name, value in update_dict.items():
                if value == "":
                    value = None  # empty string from a form clears the field
                    update_dict[field_name] = None
                elif field_name in ("next_action_due", "last_contacted_at"):
                    parsed = _parse_date(value)
                    if parsed is None:
                        raise HTTPException(status_code=400, detail=f"{field_name} must be an ISO date (YYYY-MM-DD)")
                    value = parsed
                    update_dict[field_name] = parsed
                cast = "::prospect_status" if field_name == "status" else ""
                set_clauses.append(f"{field_name} = %s{cast}")
                params.append(value)
            set_clauses.append("status_changed_by = %s")
            params.append(actor)
            if update.status == "disqualified":
                set_clauses.append("dq_reason_code = COALESCE(dq_reason_code, 'manual')")
                set_clauses.append("dq_applied_at = NOW()")
            params.append(prospect_id)
            cur.execute(f"""
                UPDATE prospect_matrix SET {', '.join(set_clauses)}
                WHERE id = %s RETURNING id, status, row_version, opportunity_stage
            """, params)
            result = cur.fetchone()

            for field_name, value in update_dict.items():
                old = before.get(field_name)
                if str(old or "") == str(value or ""):
                    continue
                kind = {"status": "status_change", "opportunity_stage": "stage_change",
                        "analyst_notes": "note"}.get(field_name, "field_change")
                log_activity(conn, prospect_id, kind, actor=actor, field=field_name, old_value=old, new_value=value)
            conn.commit()
            return {"message": "Updated", "prospect_id": str(result["id"]), "status": result["status"],
                    "row_version": result["row_version"], "opportunity_stage": result["opportunity_stage"]}
    finally:
        put_conn(conn)

# ---------------------------------------------------------------------------
# Endpoints: Watchlist
# ---------------------------------------------------------------------------

@app.patch("/api/prospects/{prospect_id}/watchlist")
def toggle_watchlist(prospect_id: str):
    """Toggle is_watchlisted for a prospect."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                UPDATE prospect_matrix
                SET is_watchlisted = NOT COALESCE(is_watchlisted, FALSE)
                WHERE id = %s
                RETURNING id, is_watchlisted
            """, (prospect_id,))
            result = cur.fetchone()
            if not result:
                raise HTTPException(status_code=404, detail="Prospect not found")
            from activity import log_activity
            log_activity(conn, prospect_id, "watchlist", field="is_watchlisted", new_value=result["is_watchlisted"])
            conn.commit()
            return {"prospect_id": str(result["id"]), "is_watchlisted": result["is_watchlisted"]}
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Endpoints: Deep Analysis (Claude-powered premium feature)
# ---------------------------------------------------------------------------

def _execute_deep_analysis(
    prospect_id: str,
    progress_callback: Optional[Callable[..., None]] = None,
    job_id: Optional[str] = None,
    force: bool = False,
):
    """Run deep analysis for one prospect and return the API response payload.
    Reuses the last analysis when the evidence pack is unchanged (unless force)."""
    if not _api_key_store["valid"]:
        raise HTTPException(
            status_code=402,
            detail="No valid API key configured. Go to Settings to add your Anthropic API key.",
        )

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if progress_callback:
                progress_callback(8, "loading_context", "Loading company context")
            cur.execute("""
                SELECT pm.id, pm.listing_id, pm.size_of_prize, pm.prize_breakdown,
                       l.ticker, l.company_name, l.gics_sector
                FROM prospect_matrix pm
                JOIN asx_listings l ON l.id = pm.listing_id
                WHERE pm.id = %s
            """, (prospect_id,))
            prospect = cur.fetchone()
            if not prospect:
                raise HTTPException(status_code=404, detail="Prospect not found")

            if progress_callback:
                progress_callback(14, "loading_signals", "Loading existing pressure signals")
            cur.execute("""
                SELECT id, pressure_type, strength, summary, source_url, source_title, model_version
                FROM pressure_signals WHERE prospect_id = %s
                ORDER BY detected_at
            """, (prospect_id,))
            existing_signals = list(cur.fetchall())

        # Extract deal_fit from prize_breakdown JSONB
        pb = prospect.get("prize_breakdown") or {}
        if isinstance(pb, str):
            import json as _json
            pb = _json.loads(pb)
        _deal_fit = pb.get("deal_fit", "") if isinstance(pb, dict) else ""

        result = _run_best_available_analysis(
            conn=conn,
            prospect_id=prospect_id,
            listing_id=str(prospect["listing_id"]),
            ticker=prospect["ticker"],
            company_name=prospect["company_name"],
            sector=prospect["gics_sector"],
            existing_signals=existing_signals,
            api_key=_api_key_store["key"],
            size_of_prize=int(prospect.get("size_of_prize") or 0),
            deal_fit=_deal_fit,
            progress_callback=progress_callback,
            job_id=job_id,
            force=force,
        )

        if "error" in result:
            raise HTTPException(status_code=result.get("http_status", 500), detail=result["error"])

        if result.get("reused"):
            with conn.cursor() as cur:
                cur.execute("SELECT prospect_score FROM prospect_matrix WHERE id = %s", (prospect_id,))
                row = cur.fetchone()
            return {
                "prospect_id": prospect_id, "ticker": prospect["ticker"], "reused": True,
                "tokens_used": 0, "new_signals_count": 0,
                "new_score": float(row[0]) if row and row[0] else 0.0,
                "validated_count": len(result.get("validated_signals", [])),
                "confirmed_count": sum(1 for v in result.get("validated_signals", []) if v.get("confirmed")),
                "disputed_count": sum(1 for v in result.get("validated_signals", []) if not v.get("confirmed")),
                "profile": result.get("refined_profile", {}),
                "validated_signals": result.get("validated_signals", []),
                "analysis_mode": result.get("analysis_mode"),
                "documents_used": result.get("documents_used", 0),
                "gap_findings": result.get("gap_findings", []),
                "executive_summary": result.get("executive_summary"),
                "message": "Evidence unchanged since the last analysis — previous result reused (no AI cost).",
            }

        if progress_callback:
            progress_callback(90, "saving_results", "Saving analysis results")
        run_id = None
        try:
            from v3_intelligence import store_intelligence_run
            run_id = store_intelligence_run(conn, prospect_id=prospect_id, listing_id=str(prospect["listing_id"]),
                                            result=result, job_id=job_id)
        except Exception as exc:
            conn.rollback()
            logger.warning("Failed to store intelligence run for %s: %s", prospect["ticker"], exc)
        inserted = _store_deep_analysis_result(
            conn,
            prospect_id=prospect_id,
            listing_id=str(prospect["listing_id"]),
            ticker=prospect["ticker"],
            existing_signals=existing_signals,
            result=result,
            run_id=run_id,
        )

        tokens_used = result.get("tokens_used", 0)
        validated = result.get("validated_signals", [])
        profile = result.get("refined_profile", {})
        _log_activity_safe(conn, prospect_id, "deep_analysis", actor="worker" if job_id else "api",
                           note=f"{result.get('analysis_mode')} — {inserted} new signals, {tokens_used} tokens",
                           payload={"job_id": job_id, "analysis_version": result.get("analysis_version")})

        # Read back updated score for the response
        if progress_callback:
            progress_callback(96, "refreshing_score", "Refreshing prospect score")
        with conn.cursor() as cur:
            cur.execute("SELECT prospect_score FROM prospect_matrix WHERE id = %s", (prospect_id,))
            row = cur.fetchone()
            new_score = float(row[0]) if row and row[0] else 0.0

        return {
            "prospect_id": prospect_id,
            "ticker": prospect["ticker"],
            "tokens_used": tokens_used,
            "validated_count": len(validated),
            "confirmed_count": sum(1 for v in validated if v.get("confirmed")),
            "disputed_count": sum(1 for v in validated if not v.get("confirmed")),
            "new_signals_count": inserted,
            "new_score": new_score,
            "profile": profile,
            "validated_signals": validated,
            "analysis_mode": result.get("analysis_mode"),
            "documents_used": result.get("documents_used", 0),
            "gap_findings": result.get("gap_findings", []),
            "executive_summary": result.get("executive_summary"),
            "validation_issues": result.get("validation_issues", []),
            "estimated_cost_usd": result.get("estimated_cost_usd"),
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.exception("Deep analysis failed for %s", prospect_id)
        raise HTTPException(status_code=500, detail=f"Deep analysis error: {e}")
    finally:
        put_conn(conn)


def _job_deep_analysis(conn, job: dict, progress):
    """Worker handler: Claude deep analysis for one prospect (job-backed)."""
    prospect_id = str((job.get("params") or {}).get("prospect_id"))

    def progress_callback(progress_pct: int, stage: str, message: str, **extra):
        progress.update({"status": "running", "progress_pct": max(0, min(int(progress_pct), 99)),
                         "stage": stage, "message": message, **extra})

    progress.update({"prospect_id": prospect_id, "status": "running", "progress_pct": 2,
                     "stage": "queued", "message": "Starting deep analysis"})
    try:
        result = _execute_deep_analysis(prospect_id, progress_callback=progress_callback, job_id=str(job["id"]),
                                        force=bool((job.get("params") or {}).get("force")))
    except HTTPException as exc:
        progress.update({"status": "failed", "stage": "failed", "message": str(exc.detail), "progress_pct": 100})
        raise RuntimeError(str(exc.detail)) from exc
    progress.update({"status": "completed", "stage": "completed", "message": "Deep analysis complete", "progress_pct": 100})
    return result


job_worker.register("deep_analysis", _job_deep_analysis)


def _deep_analysis_status_from_job(prospect_id: str, job: dict | None) -> dict:
    if not job:
        return {"prospect_id": prospect_id, "status": "idle", "progress_pct": 0, "stage": "idle",
                "message": "No deep analysis running", "updated_at": jobs.now_iso()}
    status_map = {"queued": "running", "running": "running", "succeeded": "completed",
                  "failed": "failed", "cancelled": "failed"}
    payload = dict(job.get("progress") or {})
    payload.update({
        "prospect_id": prospect_id,
        "job_id": str(job["id"]),
        "status": status_map.get(job["status"], job["status"]),
        "progress_pct": 100 if job["status"] in jobs.TERMINAL else payload.get("progress_pct", 0),
        "stage": payload.get("stage") or job["status"],
        "message": payload.get("message") or (job.get("error") or ""),
        "error": job.get("error"),
        "result": job.get("result") if job["status"] == "succeeded" else None,
        "started_at": job["started_at"].isoformat() if job.get("started_at") else None,
        "completed_at": job["finished_at"].isoformat() if job.get("finished_at") else None,
        "updated_at": job["updated_at"].isoformat() if job.get("updated_at") else jobs.now_iso(),
    })
    return payload


@app.post("/api/prospects/{prospect_id}/deep-analysis")
def deep_analysis(prospect_id: str):
    """Run Claude deep analysis for a single prospect. Synchronous (~5-10s)."""
    return _execute_deep_analysis(prospect_id)


@app.post("/api/prospects/{prospect_id}/deep-analysis/start")
def start_deep_analysis(prospect_id: str, force: bool = Query(False, description="Re-run even if evidence is unchanged")):
    """Queue deep analysis for one prospect (one active job per prospect)."""
    if not _api_key_store["valid"]:
        raise HTTPException(
            status_code=402,
            detail="No valid API key configured. Go to Settings to add your Anthropic API key.",
        )
    conn = get_conn()
    try:
        job = jobs.enqueue(conn, "deep_analysis", {"prospect_id": prospect_id, "force": force},
                           idempotency_key=f"deep_analysis:{prospect_id}", priority=3, max_attempts=1)
        return _deep_analysis_status_from_job(prospect_id, job)
    finally:
        put_conn(conn)


@app.get("/api/prospects/{prospect_id}/deep-analysis/status")
def get_deep_analysis_status(prospect_id: str):
    """Latest deep analysis job status for one prospect (read from the job table)."""
    conn = get_conn()
    try:
        job = jobs.latest_job(conn, "deep_analysis", param_match={"prospect_id": prospect_id})
        return _deep_analysis_status_from_job(prospect_id, job)
    finally:
        put_conn(conn)


@app.get("/api/prospects/{prospect_id}/documents")
def get_prospect_documents(prospect_id: str):
    """List stored full-document records for a prospect."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT listing_id
                FROM prospect_matrix
                WHERE id = %s
            """, (prospect_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Prospect not found")

            cur.execute("""
                SELECT id, source_url, source_title, source_date, document_type,
                       provider, fetch_status, fetched_at, last_error
                FROM announcement_documents
                WHERE listing_id = %s
                ORDER BY source_date DESC NULLS LAST, fetched_at DESC NULLS LAST
            """, (row["listing_id"],))
            return {"documents": cur.fetchall()}
    finally:
        put_conn(conn)


@app.post("/api/prospects/{prospect_id}/documents/collect")
def collect_prospect_documents(prospect_id: str, req: V3CollectRequest):
    """Collect and store full announcement/report content for one prospect."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT pm.id AS prospect_id, pm.listing_id,
                       l.ticker, l.company_name
                FROM prospect_matrix pm
                JOIN asx_listings l ON l.id = pm.listing_id
                WHERE pm.id = %s
            """, (prospect_id,))
            prospect = cur.fetchone()
            if not prospect:
                raise HTTPException(status_code=404, detail="Prospect not found")

            cur.execute("""
                SELECT id, pressure_type, strength, summary, source_url, source_title, source_date
                FROM pressure_signals
                WHERE prospect_id = %s
                ORDER BY detected_at DESC
            """, (prospect_id,))
            existing_signals = list(cur.fetchall())

        from v3_intelligence import collect_full_documents
        result = collect_full_documents(
            conn=conn,
            listing_id=str(prospect["listing_id"]),
            ticker=prospect["ticker"],
            existing_signals=existing_signals,
            max_documents=req.max_documents,
            force_refresh=req.force_refresh,
        )
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Endpoints: API Key Settings
# ---------------------------------------------------------------------------

@app.post("/api/settings/api-key")
def save_api_key(req: ApiKeyRequest):
    """Validate and store the Anthropic API key in memory."""
    key = req.api_key.strip()
    if not key.startswith("sk-ant-"):
        _api_key_store["key"] = key
        _api_key_store["valid"] = False
        _api_key_store["source"] = "manual"
        return {"configured": True, "valid": False, "message": "Key format invalid — must start with sk-ant-"}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        # Cheapest possible validation call
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": "hi"}],
        )
        _api_key_store["key"] = key
        _api_key_store["valid"] = True
        _api_key_store["source"] = "manual"
        logger.info("Anthropic API key validated and stored (manual)")
        return {"configured": True, "valid": True, "message": "API key validated successfully"}
    except Exception as exc:
        _api_key_store["key"] = key
        _api_key_store["valid"] = False
        return {"configured": True, "valid": False, "message": str(exc)}


@app.get("/api/settings/api-key/status")
def get_api_key_status():
    """Return whether a valid Anthropic API key is configured."""
    return {
        "configured": _api_key_store["key"] is not None,
        "valid": _api_key_store["valid"],
        "source": _api_key_store.get("source"),  # "env" or "manual" or None
    }


@app.get("/api/settings/firecrawl/status")
def get_firecrawl_api_status():
    """Return whether Firecrawl document collection is configured."""
    try:
        from v3_intelligence import get_firecrawl_status
        return get_firecrawl_status()
    except Exception:
        return {"configured": False, "valid": False, "source": None}


# ---------------------------------------------------------------------------
# Endpoints: Sectors & Stats
# ---------------------------------------------------------------------------

@app.get("/api/sectors")
def get_sector_summary():
    """Sector breakdown with prospect counts."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    l.gics_sector,
                    l.gics_industry_group,
                    COUNT(DISTINCT l.id) AS total_companies,
                    COUNT(DISTINCT pm.id) AS in_matrix,
                    COUNT(DISTINCT pm.id) FILTER (WHERE pm.status = 'enriched') AS enriched,
                    COUNT(DISTINCT pm.id) FILTER (WHERE pm.status = 'ready_for_outreach') AS ready,
                    ROUND(AVG(pm.prospect_score)::numeric, 2) AS avg_score
                FROM asx_listings l
                LEFT JOIN prospect_matrix pm ON pm.listing_id = l.id
                WHERE l.is_target_sector = TRUE AND l.is_active = TRUE
                GROUP BY l.gics_sector, l.gics_industry_group
                ORDER BY total_companies DESC
            """)
            return cur.fetchall()
    finally:
        put_conn(conn)


@app.get("/api/stats")
def get_dashboard_stats():
    """High-level dashboard statistics."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    (SELECT COUNT(*) FROM asx_listings WHERE is_active = TRUE) AS total_listings,
                    (SELECT COUNT(*) FROM asx_listings WHERE is_target_sector = TRUE AND is_active = TRUE) AS target_sector_count,
                    (SELECT COUNT(*) FROM prospect_matrix) AS total_prospects,
                    (SELECT COUNT(*) FROM prospect_matrix WHERE status = 'unscreened') AS unscreened,
                    (SELECT COUNT(*) FROM prospect_matrix WHERE status = 'qualified') AS qualified,
                    (SELECT COUNT(*) FROM prospect_matrix WHERE status = 'enriched') AS enriched,
                    (SELECT COUNT(*) FROM prospect_matrix WHERE status = 'ready_for_outreach') AS ready_for_outreach,
                    (SELECT COUNT(*) FROM prospect_matrix WHERE status = 'suggested_dq') AS suggested_dq,
                    (SELECT COUNT(*) FROM prospect_matrix WHERE status = 'disqualified') AS disqualified,
                    (SELECT COUNT(*) FROM prospect_matrix WHERE is_watchlisted = TRUE) AS watchlist_count,
                    (SELECT COUNT(*) FROM prospect_matrix WHERE lead_tier = 'hot') AS hot_leads,
                    (SELECT COUNT(*) FROM prospect_matrix WHERE lead_tier = 'warm') AS warm_leads,
                    (SELECT COUNT(*) FROM prospect_matrix WHERE lead_tier = 'watch') AS watch_leads,
                    (SELECT COUNT(*) FROM prospect_matrix WHERE prospect_score >= 7) AS high_score_count,
                    (SELECT COUNT(DISTINCT prospect_id) FROM pressure_signals WHERE strength = 'strong') AS strong_signal_companies,
                    (SELECT COUNT(*) FROM pressure_signals) AS total_signals,
                    (SELECT COUNT(*) FROM pressure_signals WHERE strength = 'strong') AS strong_signals,
                    (SELECT COUNT(DISTINCT prospect_id) FROM pressure_signals) AS has_signals_count,
                    (SELECT MAX(started_at) FROM refresh_runs WHERE status = 'completed') AS last_refresh,
                    (SELECT ROUND(AVG(prospect_score)::numeric, 2) FROM prospect_matrix WHERE prospect_score IS NOT NULL) AS avg_score
            """)
            return cur.fetchone()
    finally:
        put_conn(conn)

# ---------------------------------------------------------------------------
# Endpoints: Refresh & Enrichment
# ---------------------------------------------------------------------------

def _public_refresh_error(error) -> str:
    """Convert upstream and database exceptions into useful operator copy."""
    message = str(error or "")
    lowered = message.lower()
    if "existing listing data was not changed" in lowered:
        return message
    if "404" in lowered or "not found" in lowered:
        return "The ASX listings feed could not be found. Existing listing data was not changed."
    if "timeout" in lowered or "timed out" in lowered:
        return "ASX did not respond in time. Existing listing data was not changed."
    if "connection" in lowered or "network" in lowered:
        return "ASX could not be reached. Check the network and try again. Existing data is safe."
    return "The ASX refresh could not complete. Current dashboard data remains available."

REFRESH_PROGRESS_DEFAULT = {"running": False, "phase": "", "detail": "", "current": 0, "total": 0, "ticker": ""}


def _job_asx_refresh(conn, job: dict, progress):
    """Worker handler: ASX directory refresh + company detail pull + DQ gates.
    Logs to refresh_runs exactly as before; progress lives on the job row."""
    from asx_scraper import (
        fetch_asx_csv, parse_asx_csv, validate_listing_snapshot,
        upsert_listings, backfill_prospect_matrix, refresh_target_company_details,
        apply_gate1_dq, apply_gate2_dq,
    )
    import httpx

    triggered_by = (job.get("params") or {}).get("triggered_by") or job.get("triggered_by") or "manual"
    progress.update({**REFRESH_PROGRESS_DEFAULT, "running": True, "phase": "Fetching ASX CSV..."})
    run_id = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO refresh_runs (run_type, status, triggered_by) VALUES ('manual', 'running', %s) RETURNING id",
                (triggered_by,),
            )
            run_id = str(cur.fetchone()[0])
        conn.commit()

        progress["phase"] = "Downloading ASX listings..."
        with httpx.Client(headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-AU,en;q=0.9",
        }, timeout=30) as client:
            csv_text = fetch_asx_csv(client)

        progress["phase"] = "Parsing listings..."
        listings = parse_asx_csv(csv_text)
        validate_listing_snapshot(listings)
        progress["detail"] = f"{len(listings)} companies found"
        progress.checkpoint()

        progress["phase"] = "Updating database..."
        stats = upsert_listings(conn, listings)
        progress["detail"] = f"{stats.new_listings} new, {stats.updated_listings} updated, {stats.delisted_count} delisted"
        progress["phase"] = "Backfilling prospect matrix..."
        backfill_prospect_matrix(conn)
        progress.update({"phase": "Fetching company locations...",
                         "detail": "Pulling registered office address data from ASX company profiles",
                         "current": 0, "total": 0, "ticker": ""})

        def _location_progress(current: int, total: int, ticker: str):
            progress.update({"current": current, "total": total, "ticker": ticker,
                             "detail": f"{current}/{total} location profiles checked - {ticker}"})
            if current % 25 == 0:
                progress.checkpoint()

        refresh_target_company_details(conn, only_missing_location=True, triggered_by=triggered_by,
                                       progress_callback=_location_progress)
        conn.commit()
        progress["phase"] = "Applying data-quality gates..."
        gate1 = apply_gate1_dq(conn)
        gate2 = apply_gate2_dq(conn)

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE refresh_runs SET
                    status = 'completed', completed_at = NOW(),
                    total_listings = %s, new_listings = %s,
                    updated_listings = %s, delisted_count = %s,
                    target_sector_count = %s
                WHERE id = %s
            """, (len(listings), stats.new_listings, stats.updated_listings,
                  stats.delisted_count, stats.target_sector_count, run_id))
        conn.commit()

        progress.update({"phase": "Complete", "ticker": "", "current": progress["total"],
                         "detail": f"{len(listings)} listings — {stats.new_listings} new, {stats.target_sector_count} target sector, {gate1 + gate2} auto-DQ'd"})
        logger.info(f"Refresh complete: {len(listings)} listings")
        follow_up = _enqueue_contact_batch(conn, force=False, triggered_by="post_refresh")
        if not follow_up.get("existing"):
            logger.info("Queued post-refresh contact reconciliation (job %s)", follow_up["id"])
        return {"total_listings": len(listings), "new_listings": stats.new_listings,
                "updated_listings": stats.updated_listings, "delisted_count": stats.delisted_count,
                "target_sector_count": stats.target_sector_count, "gate1_dq": gate1, "gate2_dq": gate2,
                "refresh_run_id": run_id}
    except JobCancelled:
        conn.rollback()
        if run_id:
            with conn.cursor() as cur:
                cur.execute("UPDATE refresh_runs SET status = 'failed', completed_at = NOW(), error_message = 'cancelled by operator' WHERE id = %s", (run_id,))
            conn.commit()
        raise
    except Exception as e:
        conn.rollback()
        logger.exception("Refresh failed")
        progress.update({"phase": "Failed", "detail": _public_refresh_error(e)})
        if run_id:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE refresh_runs SET status = 'failed', completed_at = NOW(), error_message = %s WHERE id = %s",
                    (str(e)[:500], run_id),
                )
            conn.commit()
        raise
    finally:
        progress.data["running"] = False


job_worker.register("asx_refresh", _job_asx_refresh)


def _enqueue_refresh(conn, triggered_by: str) -> dict:
    return jobs.enqueue(conn, "asx_refresh", {"triggered_by": triggered_by}, idempotency_key="asx_refresh",
                        triggered_by=triggered_by, priority=4, max_attempts=1)


def _run_daily_asx_refresh_job():
    """Scheduler tick: enqueue the daily refresh (the worker executes it)."""
    conn = worker_connect()
    try:
        job = _enqueue_refresh(conn, "scheduled_auto_refresh")
        if job.get("existing"):
            logger.info("Scheduled ASX refresh skipped because a refresh job is already active")
        else:
            logger.info("Scheduled ASX refresh queued (job %s) for %02d:%02d %s",
                        job["id"], AUTO_ASX_REFRESH_HOUR, AUTO_ASX_REFRESH_MINUTE, AUTO_ASX_REFRESH_TIMEZONE)
    finally:
        conn.close()


def _run_alert_checks():
    from alerts import check_alert_conditions
    conn = worker_connect()
    try:
        fired = check_alert_conditions(conn)
        if fired:
            logger.warning("alert conditions fired: %s", ", ".join(fired))
    except Exception:
        logger.exception("alert check failed")
    finally:
        conn.close()


@app.post("/api/refresh")
def trigger_refresh(req: RefreshRequest):
    """Queue an ASX data refresh job (one active refresh at a time)."""
    conn = get_conn()
    try:
        job = _enqueue_refresh(conn, req.triggered_by)
    finally:
        put_conn(conn)
    if job.get("existing"):
        return {"message": "Refresh already running", "running": True, "job_id": str(job["id"])}
    return {"message": "Refresh queued", "triggered_by": req.triggered_by, "job_id": str(job["id"])}


@app.get("/api/refresh/status")
def get_refresh_status():
    """Current refresh progress, read from the latest refresh job."""
    conn = get_conn()
    try:
        return _job_status_payload(jobs.latest_job(conn, "asx_refresh"), REFRESH_PROGRESS_DEFAULT)
    finally:
        put_conn(conn)


@app.get("/api/refresh/latest")
def get_latest_refresh():
    """Return the most recent completed or failed refresh run."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, run_type, status, total_listings, new_listings,
                       updated_listings, delisted_count, target_sector_count,
                       error_message, started_at, completed_at, triggered_by
                FROM refresh_runs
                WHERE status IN ('completed', 'failed')
                ORDER BY started_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return {}
            result = dict(row)
            if result.get("status") == "failed":
                result["display_error"] = _public_refresh_error(result.get("error_message"))
            return result
    finally:
        put_conn(conn)


def _run_best_available_analysis(
    conn,
    prospect_id: str,
    listing_id: str,
    ticker: str,
    company_name: str,
    sector: str,
    existing_signals: list,
    api_key: str,
    size_of_prize: int = 0,
    deal_fit: str = "",
    progress_callback: Optional[Callable[..., None]] = None,
    job_id: Optional[str] = None,
    force: bool = False,
):
    """
    Prefer the V3 full-document path when Firecrawl is configured and documents
    can be collected. Fall back to the legacy headline-only analysis otherwise.

    Wrapped by evidence caching and the spend ledger:
    * if the evidence hash matches the latest stored run (and not force) the
      previous result is returned with reused=True and no provider call is made;
    * a budget reservation is taken before the call and settled with the real
      token usage afterwards. BudgetExceeded propagates to the caller.
    """
    from ai_budget import (BudgetExceeded, evidence_hash_for_prospect, find_cached_run,
                           reserve, settle, release)
    from ai_schema import validate_ai_result, AIValidationError

    ev_hash = evidence_hash_for_prospect(conn, prospect_id, listing_id)
    if not force:
        cached = find_cached_run(conn, prospect_id, ev_hash)
        if cached:
            logger.info("%s: evidence unchanged (hash %s) — reusing analysis run %s", ticker, ev_hash[:10], cached["id"])
            cached_result = dict(cached["output_json"] or {})
            cached_result.update({"reused": True, "evidence_hash": ev_hash, "tokens_used": 0})
            return cached_result

    try:
        reservation = reserve(conn, prospect_id, job_id=job_id)
    except BudgetExceeded as exc:
        if job_id:
            raise
        return {"error": str(exc), "error_code": "budget_exceeded", "http_status": 402}

    try:
        result = _run_analysis_paths(conn, prospect_id, listing_id, ticker, company_name, sector,
                                     existing_signals, api_key, size_of_prize, deal_fit, progress_callback)
    except Exception:
        release(conn, reservation)
        raise
    if "error" in result:
        release(conn, reservation)
        return result

    documents = result.get("documents_for_validation") or result.get("announcements") or []
    try:
        clean, issues = validate_ai_result(result, documents, existing_signal_count=len(existing_signals))
    except AIValidationError as exc:
        settle(conn, reservation, result, status="rejected")
        logger.warning("%s: AI output rejected by schema validation: %s", ticker, exc)
        return {"error": f"AI output failed validation and was not stored: {exc}", "error_code": "ai_validation_failed"}
    clean["validation_issues"] = issues
    clean["evidence_hash"] = ev_hash
    clean.pop("documents_for_validation", None)
    clean["estimated_cost_usd"] = settle(conn, reservation, clean)
    return clean


def _run_analysis_paths(conn, prospect_id, listing_id, ticker, company_name, sector,
                        existing_signals, api_key, size_of_prize, deal_fit, progress_callback):
    """V3 full-document path with headline fallback (unchanged behaviour)."""
    from deep_analysis import run_deep_analysis as _run_headline_deep

    try:
        from v3_intelligence import (
            collect_full_documents,
            firecrawl_is_configured,
            run_full_document_analysis,
            UI_DOCUMENT_PACK_CAP,
        )

        if firecrawl_is_configured():
            collection = collect_full_documents(
                conn=conn,
                listing_id=listing_id,
                ticker=ticker,
                existing_signals=existing_signals,
                max_documents=UI_DOCUMENT_PACK_CAP,
                force_refresh=False,
                allow_smart_expansion=False,
                progress_callback=progress_callback,
            )
            documents = collection.get("documents", [])
            if documents:
                attempted_doc_counts = []
                doc_attempts = [documents]
                for retry_limit in (4, 3, 2):
                    if len(documents) > retry_limit:
                        doc_attempts.append(documents[:retry_limit])

                result = None
                for attempt_index, attempt_docs in enumerate(doc_attempts, start=1):
                    attempted_doc_counts.append(len(attempt_docs))
                    if progress_callback:
                        retry_msg = (
                            f"Retrying with a smaller evidence pack ({len(attempt_docs)} documents)"
                            if attempt_index > 1 else
                            f"Analysing the top {len(attempt_docs)} documents"
                        )
                        progress_callback(
                            progress_pct=62,
                            stage="analysing_documents",
                            message=retry_msg,
                            attempted_doc_counts=attempted_doc_counts,
                        )
                    result = run_full_document_analysis(
                        prospect_id=prospect_id,
                        ticker=ticker,
                        company_name=company_name,
                        sector=sector,
                        existing_signals=existing_signals,
                        documents=attempt_docs,
                        api_key=api_key,
                        size_of_prize=size_of_prize,
                        deal_fit=deal_fit,
                        progress_callback=progress_callback,
                    )
                    if "error" not in result:
                        result["documents_for_validation"] = [
                            {"id": str(d.get("id")), "title": d.get("source_title"), "url": d.get("source_url")}
                            for d in attempt_docs
                        ]
                        result["document_collection"] = {
                            "requested": collection.get("requested", 0),
                            "fetched": collection.get("fetched", 0),
                            "reused": collection.get("reused", 0),
                            "failed": collection.get("failed", 0),
                            "effective_max_documents": collection.get("effective_max_documents", 0),
                            "attempted_doc_counts": attempted_doc_counts,
                            "final_documents_used": len(attempt_docs),
                        }
                        if attempt_index > 1:
                            logger.warning(
                                "%s: V3 analysis recovered after retry with %d documents",
                                ticker,
                                len(attempt_docs),
                            )
                        return result
                    if result.get("error_code") != "non_json_response":
                        break
                    logger.warning(
                        "%s: V3 parse failed with %d documents, retrying with a smaller pack",
                        ticker,
                        len(attempt_docs),
                    )
                if result is not None:
                    logger.warning("%s: V3 document analysis failed, falling back: %s", ticker, result["error"])
            else:
                logger.info("%s: no full documents collected for V3 path, falling back", ticker)
    except Exception as exc:
        logger.warning("%s: V3 path unavailable, falling back to headline analysis: %s", ticker, exc)

    if progress_callback:
        progress_callback(
            progress_pct=68,
            stage="headline_fallback",
            message="Falling back to announcement-only analysis",
        )
    result = _run_headline_deep(
        prospect_id=prospect_id,
        ticker=ticker,
        company_name=company_name,
        sector=sector,
        existing_signals=existing_signals,
        api_key=api_key,
        size_of_prize=size_of_prize,
        deal_fit=deal_fit,
    )
    result.setdefault("analysis_mode", "announcement_headlines")
    result.setdefault("analysis_version", "claude-deep-v1")
    result.setdefault("model_name", "claude-sonnet-4-6")
    return result


def _store_deep_analysis_result(conn, prospect_id: str, listing_id: str,
                                ticker: str, existing_signals: list, result: dict,
                                run_id: str | None = None) -> int:
    """
    Persist deep analysis output to the database.
    Shared by the manual endpoint and the auto-batch runner.
    Returns the number of NEW AI signals inserted (revisions of existing
    findings are updated in place and not counted).
    """
    import json as _json
    from prize_calculator import calculate_size_of_prize
    from evidence import evidence_key, ai_source_url

    valid_pt = {"production","license_to_operate","cost","people","quality","future_readiness"}
    valid_st = {"weak","moderate","strong"}
    agent_version = result.get("analysis_version") or "claude-deep-v1"
    inserted = 0
    revised = 0

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Validate/invalidate existing rule-based signals
        validated = result.get("validated_signals", [])
        for v in validated:
            idx = v.get("index", -1)
            if 0 <= idx < len(existing_signals):
                sig = existing_signals[idx]
                new_conf = 0.95 if v["confirmed"] else 0.20
                cur.execute("""
                    UPDATE pressure_signals
                    SET is_valid = %s, validated_by = %s, validated_at = NOW(), confidence_score = %s,
                        verification_status = CASE WHEN verification_status LIKE 'analyst_%%' THEN verification_status
                                                   WHEN %s THEN 'ai_confirmed' ELSE 'ai_disputed' END,
                        analysis_run_id = COALESCE(%s::uuid, analysis_run_id)
                    WHERE id = %s
                """, (v["confirmed"], agent_version, new_conf, v["confirmed"], run_id, sig["id"]))

        # Insert new AI-detected signals with stable identity
        for ns in result.get("new_signals", []):
            pt = (ns.get("pressure_type") or "").lower()
            st = (ns.get("strength") or "moderate").lower()
            if pt not in valid_pt or st not in valid_st:
                continue
            key = evidence_key(ticker, pt, ns.get("summary", ""))
            source_url = ai_source_url(ticker, pt, key, ns.get("source_url"))
            verification = ns.get("verification_status") or "ai_generated"
            is_valid = verification != "unverified"
            confidence = 0.75 if is_valid else 0.35
            try:
                cur.execute("""
                    INSERT INTO pressure_signals
                        (prospect_id, pressure_type, strength, summary,
                         source_type, source_url, source_title,
                         confidence_score, model_version, extracted_quote, is_valid,
                         evidence_key, document_id, analysis_run_id, verification_status, last_seen_at)
                    VALUES (%s,%s,%s,%s,'asx_announcement',%s,%s,
                            %s,%s,%s,%s,%s,%s::uuid,%s::uuid,%s,NOW())
                    ON CONFLICT (prospect_id, pressure_type, source_url) DO UPDATE SET
                        revision_count = pressure_signals.revision_count + 1,
                        last_seen_at = NOW(),
                        analysis_run_id = COALESCE(EXCLUDED.analysis_run_id, pressure_signals.analysis_run_id),
                        document_id = COALESCE(pressure_signals.document_id, EXCLUDED.document_id),
                        evidence_key = COALESCE(pressure_signals.evidence_key, EXCLUDED.evidence_key),
                        model_version = EXCLUDED.model_version,
                        -- a cited real URL confirms an existing rule-engine signal from the same announcement
                        verification_status = CASE WHEN pressure_signals.verification_status LIKE 'analyst_%%'
                                                   THEN pressure_signals.verification_status
                                                   WHEN pressure_signals.source_url LIKE 'claude-deep://%%'
                                                   THEN pressure_signals.verification_status
                                                   ELSE 'ai_confirmed' END,
                        confidence_score = GREATEST(pressure_signals.confidence_score, EXCLUDED.confidence_score)
                    RETURNING (xmax = 0) AS inserted_row
                """, (prospect_id, pt, st, ns.get("summary",""),
                      source_url, ns.get("source_title",""), confidence, agent_version,
                      ns.get("reasoning",""), is_valid, key, ns.get("document_id"), run_id, verification))
                row = cur.fetchone()
                if row and row["inserted_row"]:
                    inserted += 1
                else:
                    revised += 1
            except Exception as exc:
                logger.error("Failed to insert AI signal for %s: %s", ticker, exc)
                raise

        # Update strategic profile + extended deep analysis fields
        profile = result.get("refined_profile", {})
        if profile:
            lk = max(1, min(10, int(profile.get("likelihood_score") or 5)))
            cur.execute("""
                UPDATE prospect_matrix SET
                    strategic_direction  = COALESCE(%s, strategic_direction),
                    primary_tailwind     = COALESCE(%s, primary_tailwind),
                    primary_headwind     = COALESCE(%s, primary_headwind),
                    likelihood_score     = %s,
                    key_pressures        = COALESCE(%s, key_pressures),
                    nd_fit_assessment    = COALESCE(%s, nd_fit_assessment),
                    outreach_hypothesis  = COALESCE(%s, outreach_hypothesis),
                    red_flags            = COALESCE(%s, red_flags),
                    prize_ai_assessment  = COALESCE(%s, prize_ai_assessment)
                WHERE id = %s
            """, (
                profile.get("strategic_direction"), profile.get("primary_tailwind"),
                profile.get("primary_headwind"), lk,
                profile.get("key_pressures"), profile.get("nd_fit_assessment"),
                result.get("outreach_hypothesis"), result.get("red_flags"),
                result.get("prize_assessment"), prospect_id,
            ))

        # Recalculate score AND tier from the same persisted evidence set
        cur.execute("SELECT calculate_prospect_score(%s)", (prospect_id,))
        cur.execute("SELECT calculate_lead_tier(%s)", (prospect_id,))

        # Recalculate prize now that new AI signals may have been added
        try:
            prize = calculate_size_of_prize(conn, prospect_id)
            cur.execute("""
                UPDATE prospect_matrix SET size_of_prize = %s, prize_breakdown = %s
                WHERE id = %s
            """, (prize["total_prize"], _json.dumps(prize, default=str), prospect_id))
        except Exception as exc:
            logger.warning("Prize recalc failed after deep analysis for %s: %s", ticker, exc)

        # Audit log
        tokens_used = result.get("tokens_used", 0)
        anns = result.get("announcements", [])
        confirmed_count = sum(1 for v in validated if v.get("confirmed"))
        cur.execute("""
            INSERT INTO enrichment_log
                (listing_id, action, source_type, success,
                 documents_processed, signals_found,
                 triggered_by, completed_at, agent_version, tokens_used)
            VALUES (%s,'deep_analysis','asx_announcement',TRUE,%s,%s,
                    'auto_batch',NOW(),%s,%s)
        """, (listing_id, len(anns), confirmed_count + inserted, agent_version, tokens_used))

        conn.commit()

    logger.info("%s: deep analysis stored — %d new signals, %d revisions, %d tokens", ticker, inserted, revised, tokens_used)
    result["revised_signals_count"] = revised
    return inserted


def _auto_deep_analysis(conn, progress):
    """
    Run Deep Analysis on the top 10 prospects by prospect score.
    Called automatically after Enrich All if an Anthropic API key is configured.
    Writes ai_* progress keys so the frontend can show the AI phase.
    """
    import time as _time
    from ai_budget import BudgetExceeded

    progress["ai_selection_basis"] = "prospect_score"

    if not _api_key_store.get("valid") or not _api_key_store.get("key"):
        progress.update({"ai_status": "skipped_no_api_key",
                         "ai_message": "AI deep analysis skipped: no valid Anthropic API key configured."})
        logger.info("Auto deep analysis skipped - no valid API key configured")
        return

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT pm.id AS prospect_id, pm.listing_id,
                   pm.size_of_prize, pm.prize_breakdown, pm.prospect_score,
                   l.ticker, l.company_name, l.gics_sector,
                   COUNT(ps.id) AS signal_count
            FROM prospect_matrix pm
            JOIN asx_listings l ON l.id = pm.listing_id
            LEFT JOIN pressure_signals ps ON ps.prospect_id = pm.id
            WHERE l.is_active = TRUE AND l.is_target_sector = TRUE
              AND pm.status NOT IN ('disqualified', 'archived')
            GROUP BY pm.id, pm.listing_id, pm.size_of_prize, pm.prize_breakdown,
                     pm.prospect_score, l.ticker, l.company_name, l.gics_sector
            HAVING COUNT(ps.id) > 0
            ORDER BY pm.prospect_score DESC NULLS LAST, signal_count DESC NULLS LAST
            LIMIT 10
        """)
        top_10 = cur.fetchall()

    if not top_10:
        progress.update({"ai_status": "skipped_no_candidates",
                         "ai_message": "AI deep analysis skipped: no scored prospects with signals were available."})
        return

    progress.update({"ai_running": True, "ai_total": len(top_10), "ai_current": 0, "ai_ticker": "",
                     "ai_status": "running",
                     "ai_message": f"Running AI deep analysis on top {len(top_10)} prospects by score."})
    logger.info("Auto deep analysis starting on %d top prospects by score", len(top_10))

    for i, p in enumerate(top_10):
        tk = p["ticker"]
        prospect_id = str(p["prospect_id"])
        listing_id = str(p["listing_id"])
        progress.update({"ai_current": i + 1, "ai_ticker": tk})
        progress.checkpoint()

        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, pressure_type, strength, summary, source_url,
                           source_title, model_version
                    FROM pressure_signals WHERE prospect_id = %s
                    ORDER BY detected_at
                """, (prospect_id,))
                existing_signals = list(cur.fetchall())

            pb = p.get("prize_breakdown") or {}
            if isinstance(pb, str):
                import json as _json2
                pb = _json2.loads(pb)
            deal_fit = pb.get("deal_fit", "") if isinstance(pb, dict) else ""

            result = _run_best_available_analysis(
                conn=conn,
                prospect_id=prospect_id,
                listing_id=listing_id,
                ticker=tk,
                company_name=p["company_name"],
                sector=p["gics_sector"],
                existing_signals=existing_signals,
                api_key=_api_key_store["key"],
                size_of_prize=int(p.get("size_of_prize") or 0),
                deal_fit=deal_fit,
                job_id=progress.job_id,
            )

            if "error" in result:
                progress.data["ai_fail"] += 1
                progress["ai_message"] = f"AI analysis failed for {tk}: {result['error']}"
                logger.warning("Auto deep analysis error for %s: %s", tk, result["error"])
            elif result.get("reused"):
                progress.data["ai_skip"] += 1
                progress["ai_message"] = f"AI analysis reused for {tk} (evidence unchanged)"
            else:
                run_id = None
                try:
                    from v3_intelligence import store_intelligence_run
                    run_id = store_intelligence_run(conn, prospect_id, listing_id, result, job_id=progress.job_id)
                except Exception as exc:
                    conn.rollback()
                    logger.warning("Failed to store intelligence run for %s: %s", tk, exc)
                _store_deep_analysis_result(conn, prospect_id, listing_id, tk, existing_signals, result, run_id=run_id)
                _log_activity_safe(conn, prospect_id, "deep_analysis", actor="worker",
                                   note=f"auto batch — {result.get('analysis_mode')}", payload={"job_id": progress.job_id})
                progress.data["ai_ok"] += 1
                progress["ai_message"] = f"AI analysis stored for {tk}"
        except BudgetExceeded as exc:
            progress.update({"ai_status": "stopped_budget", "ai_message": str(exc)})
            logger.warning("Auto deep analysis stopped: %s", exc)
            break
        except Exception as exc:
            conn.rollback()
            progress.data["ai_fail"] += 1
            progress["ai_message"] = f"AI analysis failed for {tk}: {exc}"
            logger.error("Auto deep analysis failed for %s: %s", tk, exc)

        if i < len(top_10) - 1:
            _time.sleep(2.0)

    progress.data["ai_running"] = False
    progress.data["ai_ticker"] = ""
    if progress["ai_status"] != "stopped_budget":
        progress.data["ai_status"] = "completed_with_errors" if progress["ai_fail"] else "completed"
        progress.data["ai_message"] = (
            f"AI deep analysis complete: {progress['ai_ok']} succeeded, "
            f"{progress['ai_fail']} failed, {progress['ai_skip']} reused."
        )
    progress.flush(force=True)
    logger.info("Auto deep analysis complete")


def _build_prospect_filters(sector=None, lead_tier=None, watchlist=None,
                            has_signals=None, min_prize=None, min_score=None,
                            australia_only=False, city=None, exclude_dq=False):
    """Build WHERE clauses + params for prospect filtering. Shared by list + batch endpoints."""
    where_clauses = ["l.is_active = TRUE", "l.is_target_sector = TRUE"]
    where_params = []
    if sector:
        where_clauses.append("l.gics_sector = %s")
        where_params.append(sector)
    if lead_tier:
        where_clauses.append("pm.lead_tier = %s")
        where_params.append(lead_tier)
    if min_score is not None:
        where_clauses.append("pm.prospect_score >= %s")
        where_params.append(min_score)
    if min_prize is not None:
        where_clauses.append("pm.size_of_prize >= %s")
        where_params.append(min_prize)
    if australia_only:
        where_clauses.append("pm.in_australia = TRUE")
    if city:
        cities = [c.strip() for c in city.split(",") if c.strip()]
        if cities:
            placeholders = ",".join(["%s"] * len(cities))
            where_clauses.append(f"pm.registered_city IN ({placeholders})")
            where_params.extend(cities)
    if watchlist:
        where_clauses.append("pm.is_watchlisted = TRUE")
    if exclude_dq:
        where_clauses.append("pm.status NOT IN ('disqualified', 'archived')")
    having_sql = ""
    if has_signals:
        having_sql = "HAVING COUNT(ps.id) > 0"
    return " AND ".join(where_clauses), where_params, having_sql


ENRICH_PROGRESS_DEFAULT = {
    "running": False, "current": 0, "total": 0, "ticker": "", "ok": 0, "skip": 0, "fail": 0,
    "ai_running": False, "ai_current": 0, "ai_total": 0, "ai_ticker": "",
    "ai_ok": 0, "ai_fail": 0, "ai_skip": 0,
    "ai_status": "idle", "ai_message": "", "ai_selection_basis": "prospect_score",
}


def _job_enrich_batch(conn, job: dict, progress):
    """Worker handler: rule-based enrichment for all (or filtered) target prospects,
    then the AI phase on the top 10. Resumable via progress['done']."""
    from enrichment_agent import detect_signals, generate_profile, save_results
    from asx_browser import ASXFetcher
    import time as _time

    filters = (job.get("params") or {}).get("filters") or {}
    done = set(progress.get("done") or [])
    carried = {k: progress.get(k, ENRICH_PROGRESS_DEFAULT[k]) for k in ("ok", "skip", "fail")}
    progress.update({**ENRICH_PROGRESS_DEFAULT, **carried, "running": True, "done": sorted(done)})

    where_sql, where_params, having_sql = _build_prospect_filters(**filters)
    query = f"""
        SELECT pm.id prospect_id, pm.status, l.id listing_id,
               l.ticker, l.company_name, l.gics_sector,
               l.principal_activities
        FROM prospect_matrix pm JOIN asx_listings l ON l.id=pm.listing_id
        LEFT JOIN pressure_signals ps ON ps.prospect_id = pm.id
        WHERE {where_sql}
        GROUP BY pm.id, l.id, l.ticker, l.company_name, l.gics_sector, l.principal_activities, l.market_cap_aud
        {having_sql}
        ORDER BY l.market_cap_aud DESC NULLS LAST
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, where_params)
        prospects = cur.fetchall()
    progress["total"] = len(prospects)
    logger.info(f"Batch enrichment started: {len(prospects)} prospects ({len(done)} already done)")

    with ASXFetcher() as fetcher:
        for i, p in enumerate(prospects):
            tk = p["ticker"]
            progress.data["current"] = i + 1
            if tk in done:
                continue
            progress["ticker"] = tk
            try:
                anns = fetcher.fetch_announcements(tk)
                _time.sleep(2.0)
                if not anns:
                    progress.data["skip"] += 1
                else:
                    signals = detect_signals(p["company_name"], anns)
                    profile = generate_profile(p["gics_sector"], signals)
                    save_results(conn, dict(p), signals, profile, anns, p.get("principal_activities") or "")
                    progress.data["ok"] += 1
            except Exception as e:
                conn.rollback()
                logger.error(f"{tk}: {e}")
                progress.data["fail"] += 1
            done.add(tk)
            progress.data["done"] = sorted(done)
            progress.checkpoint()

    progress.update({"ticker": "", "running": False})
    _auto_deep_analysis(conn, progress)
    progress.data["running"] = False
    progress.flush(force=True)
    logger.info(f"Batch enrichment finished: {progress['ok']} ok, {progress['skip']} skip, {progress['fail']} fail")
    return {"total": len(prospects), "ok": progress["ok"], "skip": progress["skip"], "fail": progress["fail"],
            "ai_ok": progress["ai_ok"], "ai_fail": progress["ai_fail"], "ai_skip": progress["ai_skip"],
            "ai_status": progress["ai_status"]}


job_worker.register("enrich_batch", _job_enrich_batch)


def _job_enrich_single(conn, job: dict, progress):
    from enrichment_agent import run_single
    ticker = str((job.get("params") or {}).get("ticker") or "").upper()
    progress.update({"ticker": ticker, "message": f"Enriching {ticker}"})
    run_single(ticker)
    return {"ticker": ticker}


job_worker.register("enrich_single", _job_enrich_single)


@app.post("/api/enrich/batch")
def trigger_batch_enrichment(
    sector: Optional[str] = Query(None),
    lead_tier: Optional[str] = Query(None),
    watchlist: Optional[bool] = Query(None),
    has_signals: Optional[bool] = Query(None),
    min_prize: Optional[int] = Query(None),
    min_score: Optional[float] = Query(None),
    australia_only: bool = Query(False),
    city: Optional[str] = Query(None),
    exclude_dq: bool = Query(False),
):
    """Queue batch enrichment for target sector prospects.
    Accepts the same filters as /api/prospects so you can enrich a focused subset.
    The response includes the estimated AI cost of the follow-up deep-analysis phase."""
    filters = {k: v for k, v in dict(
        sector=sector, lead_tier=lead_tier, watchlist=watchlist,
        has_signals=has_signals, min_prize=min_prize, min_score=min_score,
        australia_only=australia_only, city=city, exclude_dq=exclude_dq,
    ).items() if v not in (None, False, 0)}

    where_sql, where_params, having_sql = _build_prospect_filters(**filters)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if having_sql:
                cur.execute(
                    f"SELECT COUNT(*) FROM ("
                    f"  SELECT pm.id FROM prospect_matrix pm JOIN asx_listings l ON l.id=pm.listing_id "
                    f"  LEFT JOIN pressure_signals ps ON ps.prospect_id = pm.id "
                    f"  WHERE {where_sql} GROUP BY pm.id {having_sql}"
                    f") sub",
                    where_params,
                )
            else:
                cur.execute(
                    f"SELECT COUNT(*) FROM prospect_matrix pm JOIN asx_listings l ON l.id=pm.listing_id "
                    f"WHERE {where_sql}",
                    where_params,
                )
            count = cur.fetchone()[0]
        if count == 0:
            return {"message": "No prospects match these filters", "count": 0}

        from ai_budget import estimate_batch
        ai_estimate = estimate_batch(conn, companies=10) if _api_key_store["valid"] else None
        job = jobs.enqueue(conn, "enrich_batch", {"filters": filters}, idempotency_key="enrich_batch",
                           triggered_by="api", priority=6, max_attempts=2)
    finally:
        put_conn(conn)

    if job.get("existing"):
        return {"message": "Enrichment already running", "count": (job.get("progress") or {}).get("total", count),
                "running": True, "job_id": str(job["id"])}
    label = "filtered prospects" if filters else "companies"
    return {"message": f"Enrichment queued for {count} {label}", "count": count, "job_id": str(job["id"]),
            "ai_estimate": ai_estimate}


@app.get("/api/enrich/status")
def get_enrichment_status():
    """Current enrichment progress (latest batch job), including the AI phase."""
    conn = get_conn()
    try:
        return _job_status_payload(jobs.latest_job(conn, "enrich_batch"), ENRICH_PROGRESS_DEFAULT)
    finally:
        put_conn(conn)


@app.get("/api/signals/{signal_id}/source-pdf")
def get_signal_source_pdf(signal_id: str):
    """Fetch the ASX announcement page and return it as a PDF via Playwright."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT source_url, source_title FROM pressure_signals WHERE id = %s", (signal_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Signal not found")
            if not row["source_url"] or row["source_url"].startswith("claude-deep://"):
                raise HTTPException(status_code=400, detail="No ASX source URL available for this signal")
    finally:
        put_conn(conn)

    source_url = row["source_url"]
    title = (row["source_title"] or "announcement").replace(" ", "_")[:50]
    try:
        from safe_http import validate_public_url
        validate_public_url(source_url, allowed_hosts=("asx.com.au", "markitdigital.com", "asx.api.markitdigital.com"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Source URL not allowed: {exc}")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(source_url, wait_until="networkidle", timeout=15000)
            pdf_bytes = page.pdf(format="A4", print_background=True)
            browser.close()
    except Exception as e:
        logger.error("PDF generation failed for %s: %s", source_url, e)
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")

    from fastapi.responses import Response
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{title}.pdf"'},
    )


@app.post("/api/enrich/{ticker}")
def trigger_enrichment(ticker: str):
    """Queue enrichment for a single company."""
    conn = get_conn()
    try:
        job = jobs.enqueue(conn, "enrich_single", {"ticker": ticker.upper()},
                           idempotency_key=f"enrich_single:{ticker.upper()}", priority=2, max_attempts=1)
    finally:
        put_conn(conn)
    return {"message": f"Enrichment {'already running' if job.get('existing') else 'queued'} for {ticker.upper()}",
            "job_id": str(job["id"])}


# ---------------------------------------------------------------------------
# Endpoints: Background jobs
# ---------------------------------------------------------------------------

def _strip_done(job: dict) -> dict:
    progress = dict(job.get("progress") or {})
    progress.pop("done", None)
    return {**job, "progress": progress}


@app.get("/api/jobs")
def list_background_jobs(job_type: Optional[str] = Query(None), status: Optional[str] = Query(None),
                         limit: int = Query(50, ge=1, le=200)):
    conn = get_conn()
    try:
        rows = jobs.list_jobs(conn, job_type=job_type, status=status, limit=limit)
        return {"jobs": [jobs.job_public(_strip_done(r)) for r in rows], "stats": jobs.queue_stats(conn),
                "worker_in_process": RUN_WORKER, "registered_types": job_worker.registered_types()}
    finally:
        put_conn(conn)


@app.get("/api/jobs/{job_id}")
def get_background_job(job_id: str):
    conn = get_conn()
    try:
        job = jobs.get_job(conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return jobs.job_public(_strip_done(job))
    finally:
        put_conn(conn)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_background_job(job_id: str):
    conn = get_conn()
    try:
        job = jobs.cancel(conn, job_id)
        if not job:
            existing = jobs.get_job(conn, job_id)
            if not existing:
                raise HTTPException(status_code=404, detail="Job not found")
            return {"message": f"Job is already {existing['status']}", **jobs.job_public(_strip_done(existing))}
        return {"message": "Cancellation requested" if job["status"] == "running" else "Job cancelled",
                **jobs.job_public(_strip_done(job))}
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Endpoints: Scheduled Cron Enrichment
# ---------------------------------------------------------------------------

@app.post("/api/cron/enrich-all")
def cron_enrich_all(token: str = Query(None)):
    """
    Full ASX scrape + enrichment cycle for scheduled cron jobs.
    Protected by CRON_SECRET token to prevent unauthorized access.
    """
    if not CRON_SECRET:
        raise HTTPException(
            status_code=503,
            detail="CRON_SECRET not configured — set it in environment variables to enable this endpoint",
        )
    if not token or not secrets.compare_digest(token, CRON_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing cron token")

    logger.info("Cron enrich-all triggered")
    conn = get_conn()
    try:
        refresh = _enqueue_refresh(conn, "cron")
        enrich = jobs.enqueue(conn, "enrich_batch", {"filters": {"exclude_dq": True}}, idempotency_key="enrich_batch",
                              triggered_by="cron", priority=6)
    finally:
        put_conn(conn)
    return {
        "status": "queued",
        "refresh_job_id": str(refresh["id"]), "refresh_already_running": bool(refresh.get("existing")),
        "enrich_job_id": str(enrich["id"]), "enrich_already_running": bool(enrich.get("existing")),
        "note": "Jobs run in priority order on the worker: refresh first, then enrichment.",
    }


@app.get("/api/refresh/history")
def get_refresh_history(limit: int = Query(10, ge=1, le=50)):
    """Recent refresh run history."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT *
                FROM refresh_runs
                ORDER BY started_at DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()
    finally:
        put_conn(conn)

# ---------------------------------------------------------------------------
# Endpoints: Search
# ---------------------------------------------------------------------------

@app.get("/api/search")
def search_companies(
    q: str = Query(..., min_length=1, description="Search query"),
    target_only: bool = Query(True, description="Only target sector companies"),
    limit: int = Query(20, ge=1, le=100),
):
    """Search companies by name or ticker with fuzzy matching."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            where = ["is_active = TRUE"]
            params = []
            
            if target_only:
                where.append("is_target_sector = TRUE")
            
            where.append("(company_name ILIKE %s OR ticker ILIKE %s)")
            search_term = f"%{q}%"
            params.extend([search_term, search_term])
            
            cur.execute(f"""
                SELECT id, ticker, company_name, gics_sector, gics_industry_group,
                       market_cap_aud, is_target_sector
                FROM asx_listings
                WHERE {' AND '.join(where)}
                ORDER BY 
                    CASE WHEN ticker ILIKE %s THEN 0 ELSE 1 END,
                    market_cap_aud DESC NULLS LAST
                LIMIT %s
            """, params + [f"{q}%", limit])
            
            return cur.fetchall()
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Endpoints: PitchBook Snapshots (manual enrichment, ~10/week)
# ---------------------------------------------------------------------------

@app.post("/api/prospects/{prospect_id}/snapshot")
def upsert_snapshot(prospect_id: str, body: SnapshotUpsert):
    """UPSERT a PitchBook snapshot for a prospect. Auth required when credentials are configured."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            as_of = _parse_date((body.snapshot_data or {}).get("as_of"))
            cur.execute("""
                INSERT INTO pitchbook_snapshots
                    (prospect_id, snapshot_data, snapshot_markdown, enriched_by, source_as_of, edited_at)
                VALUES (%s, %s, %s, 'manual', %s, NOW())
                ON CONFLICT (prospect_id) DO UPDATE SET
                    snapshot_data     = EXCLUDED.snapshot_data,
                    snapshot_markdown = EXCLUDED.snapshot_markdown,
                    enriched_at       = NOW(),
                    enriched_by       = 'manual',
                    source_as_of      = COALESCE(EXCLUDED.source_as_of, pitchbook_snapshots.source_as_of),
                    edited_at         = NOW()
                RETURNING *
            """, (prospect_id, Json(body.snapshot_data), body.snapshot_markdown, as_of))
            conn.commit()
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Prospect not found")
            from contact_enrichment import sync_snapshot_contacts

            sync_snapshot_contacts(conn, prospect_id)
            _log_activity_safe(conn, prospect_id, "snapshot", actor="api", note="snapshot saved (manual)")
            return row
    finally:
        put_conn(conn)


@app.get("/api/prospects/{prospect_id}/snapshot")
def get_snapshot(prospect_id: str):
    """Return the PitchBook snapshot for a prospect, or 404 if none exists."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM pitchbook_snapshots WHERE prospect_id = %s",
                (prospect_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="No snapshot found for this prospect")
            return row
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Endpoints: Data-quality review (Phase 2)
# ---------------------------------------------------------------------------

GATE1_CAP_CENTS = 500_000_000        # AUD $5M
GATE2_CAP_CENTS = 5_000_000_000      # AUD $50M


def _dq_review_rows(conn, limit: int = 200) -> list[dict]:
    """Disqualified prospects whose latest market data no longer satisfies the
    gate that removed them. Nothing is changed automatically — an analyst decides."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT pm.id AS prospect_id, l.ticker, l.company_name, l.gics_sector,
                   pm.dq_reason_code, pm.dq_reason, pm.dq_applied_at,
                   pm.dq_observed_cap_cents, pm.dq_observed_volume,
                   l.market_cap_aud AS current_cap_cents, l.day_volume AS current_volume,
                   l.detail_fetched_at,
                   CASE
                     WHEN pm.dq_reason_code = 'gate1_market_cap' AND l.market_cap_aud >= %s THEN 'market cap recovered above the $5M floor'
                     WHEN pm.dq_reason_code = 'gate2_zero_volume' AND (l.day_volume > 0 OR l.market_cap_aud >= %s) THEN 'trading volume or market cap recovered'
                     ELSE NULL
                   END AS review_reason
            FROM prospect_matrix pm
            JOIN asx_listings l ON l.id = pm.listing_id
            WHERE pm.status = 'disqualified'
              AND l.is_active = TRUE
              AND pm.dq_review_dismissed_at IS NULL
              AND (
                   (pm.dq_reason_code = 'gate1_market_cap' AND l.market_cap_aud >= %s)
                OR (pm.dq_reason_code = 'gate2_zero_volume' AND (l.day_volume > 0 OR l.market_cap_aud >= %s))
              )
            ORDER BY l.market_cap_aud DESC NULLS LAST
            LIMIT %s
        """, (GATE1_CAP_CENTS, GATE2_CAP_CENTS, GATE1_CAP_CENTS, GATE2_CAP_CENTS, limit))
        return [dict(r) for r in cur.fetchall()]


@app.get("/api/dq/review-queue")
def dq_review_queue(limit: int = Query(200, ge=1, le=1000)):
    conn = get_conn()
    try:
        rows = _dq_review_rows(conn, limit)
        return {"items": rows, "total": len(rows),
                "thresholds": {"gate1_cap_cents": GATE1_CAP_CENTS, "gate2_cap_cents": GATE2_CAP_CENTS}}
    finally:
        put_conn(conn)


class DQDecision(BaseModel):
    note: Optional[str] = None
    actor: str = "analyst"


@app.post("/api/prospects/{prospect_id}/dq/reinstate")
def dq_reinstate(prospect_id: str, body: DQDecision):
    """Analyst reverses an automatic DQ. Status returns to 'qualified'; the
    original DQ observation stays in the activity log."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT status, dq_reason, dq_reason_code FROM prospect_matrix WHERE id = %s", (prospect_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Prospect not found")
            if row["status"] != "disqualified":
                raise HTTPException(status_code=409, detail=f"Prospect is {row['status']}, not disqualified")
            cur.execute("""
                UPDATE prospect_matrix
                SET status = 'qualified', status_changed_by = %s, dq_reason = NULL, dq_reason_code = NULL,
                    dq_review_dismissed_at = NULL
                WHERE id = %s RETURNING status, row_version
            """, (body.actor, prospect_id))
            out = cur.fetchone()
        from activity import log_activity
        log_activity(conn, prospect_id, "dq_reinstated", actor=body.actor, field="status",
                     old_value="disqualified", new_value="qualified", note=body.note,
                     payload={"previous_dq_reason": row["dq_reason"], "previous_dq_reason_code": row["dq_reason_code"]})
        conn.commit()
        return {"prospect_id": prospect_id, "status": out["status"], "row_version": out["row_version"]}
    finally:
        put_conn(conn)


@app.post("/api/prospects/{prospect_id}/dq/dismiss-review")
def dq_dismiss_review(prospect_id: str, body: DQDecision):
    """Keep the DQ but remove the company from the review queue."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE prospect_matrix SET dq_review_dismissed_at = NOW() WHERE id = %s AND status = 'disqualified'",
                        (prospect_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Disqualified prospect not found")
        from activity import log_activity
        log_activity(conn, prospect_id, "dq_review_dismissed", actor=body.actor, note=body.note)
        conn.commit()
        return {"prospect_id": prospect_id, "dismissed": True}
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Endpoints: Scoring policy + AI spend (Phase 3)
# ---------------------------------------------------------------------------

class ScorePolicyUpdate(BaseModel):
    version: str = Field(pattern="^(v1|v2)$")
    rescore: bool = True


@app.get("/api/settings/score-policy")
def get_score_policy():
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT value, updated_at FROM app_settings WHERE key = 'score_policy_version'")
            row = cur.fetchone() or {"value": "v1", "updated_at": None}
            cur.execute("""
                SELECT COUNT(*) FILTER (WHERE score_v1 IS NOT NULL) AS scored,
                       ROUND(AVG(score_v1)::numeric, 2) AS avg_v1, ROUND(AVG(score_v2)::numeric, 2) AS avg_v2,
                       COUNT(*) FILTER (WHERE score_v1 IS NOT NULL AND score_v2 IS NOT NULL
                                        AND ABS(score_v1 - score_v2) > GREATEST(score_v1, 1) * 0.5) AS large_moves
                FROM prospect_matrix pm JOIN asx_listings l ON l.id = pm.listing_id
                WHERE l.is_active AND l.is_target_sector
            """)
            stats = dict(cur.fetchone())
        return {"version": row["value"], "updated_at": row["updated_at"], "comparison": stats,
                "policies": {
                    "v1": "Σ(strength × pillar weight) × likelihood/10. No recency, no per-event damping.",
                    "v2": "Σ(strength × pillar × recency × confidence × 0.6^(rank-1 per pillar)) × (0.7 + 0.06×likelihood). Explainable; stale and repeated evidence count less.",
                }}
    finally:
        put_conn(conn)


@app.put("/api/settings/score-policy")
def set_score_policy(body: ScorePolicyUpdate):
    """Switch the displayed score between v1 and v2 (both are always computed)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO app_settings (key, value) VALUES ('score_policy_version', %s)
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""", (body.version,))
            rescored = 0
            if body.rescore:
                cur.execute("""SELECT pm.id FROM prospect_matrix pm JOIN asx_listings l ON l.id = pm.listing_id
                               WHERE l.is_active AND l.is_target_sector""")
                ids = [r[0] for r in cur.fetchall()]
                for pid in ids:
                    cur.execute("SELECT calculate_prospect_score(%s)", (pid,))
                rescored = len(ids)
        conn.commit()
        from alerts import notify
        notify(conn, "score_policy_changed", f"Displayed score policy set to {body.version} ({rescored} prospects rescored)",
               payload={"version": body.version}, severity="info")
        return {"version": body.version, "rescored": rescored}
    finally:
        put_conn(conn)


@app.post("/api/prospects/{prospect_id}/rescore")
def rescore_prospect(prospect_id: str):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT calculate_prospect_score(%s) AS score, calculate_lead_tier(%s) AS tier", (prospect_id, prospect_id))
            row = cur.fetchone()
            cur.execute("SELECT score_breakdown, score_v1, score_v2, score_policy_version FROM prospect_matrix WHERE id = %s", (prospect_id,))
            detail = cur.fetchone()
        conn.commit()
        if not detail:
            raise HTTPException(status_code=404, detail="Prospect not found")
        return {"prospect_id": prospect_id, "prospect_score": row["score"], "lead_tier": row["tier"], **detail}
    finally:
        put_conn(conn)


@app.get("/api/ai/estimate")
def ai_estimate(companies: int = Query(10, ge=1, le=200)):
    from ai_budget import estimate_batch
    conn = get_conn()
    try:
        return {**estimate_batch(conn, companies=companies), "api_key_configured": _api_key_store["valid"]}
    finally:
        put_conn(conn)


@app.get("/api/ai/ledger")
def ai_ledger(months: int = Query(6, ge=1, le=36)):
    from ai_budget import ledger_summary, month_spend
    conn = get_conn()
    try:
        return {"months": ledger_summary(conn, months), "current": month_spend(conn)}
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Endpoints: Analyst workflow (Phase 4)
# ---------------------------------------------------------------------------

class SignalReview(BaseModel):
    is_valid: bool
    note: Optional[str] = None
    actor: str = "analyst"


@app.patch("/api/signals/{signal_id}")
def review_signal(signal_id: str, body: SignalReview):
    """Analyst accepts or rejects a signal. Score and tier are recalculated from
    the same persisted evidence set."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                UPDATE pressure_signals
                SET is_valid = %s, validated_by = %s, validated_at = NOW(), analyst_note = %s,
                    verification_status = CASE WHEN %s THEN 'analyst_confirmed' ELSE 'analyst_rejected' END,
                    confidence_score = CASE WHEN %s THEN GREATEST(COALESCE(confidence_score, 0), 0.9) ELSE LEAST(COALESCE(confidence_score, 1), 0.2) END
                WHERE id = %s
                RETURNING prospect_id, pressure_type, summary
            """, (body.is_valid, body.actor, body.note, body.is_valid, body.is_valid, signal_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Signal not found")
            pid = str(row["prospect_id"])
            cur.execute("SELECT calculate_prospect_score(%s) AS score, calculate_lead_tier(%s) AS tier", (pid, pid))
            scored = cur.fetchone()
        from activity import log_activity
        log_activity(conn, pid, "signal_review", actor=body.actor, field=str(row["pressure_type"]),
                     new_value="accepted" if body.is_valid else "rejected", note=body.note,
                     payload={"signal_id": signal_id, "summary": row["summary"]})
        conn.commit()
        return {"signal_id": signal_id, "prospect_id": pid, "is_valid": body.is_valid,
                "prospect_score": scored["score"], "lead_tier": scored["tier"]}
    finally:
        put_conn(conn)


@app.get("/api/prospects/{prospect_id}/activity")
def get_prospect_activity(prospect_id: str, limit: int = Query(100, ge=1, le=500)):
    from activity import recent_activity
    conn = get_conn()
    try:
        return {"activity": recent_activity(conn, prospect_id, limit)}
    finally:
        put_conn(conn)


@app.get("/api/review-queue")
def review_queue(limit: int = Query(100, ge=1, le=500), owner: Optional[str] = Query(None)):
    """Daily queue: what needs an analyst's attention and why.
    Reasons: overdue_action, new_evidence, stale_research, dq_review."""
    conn = get_conn()
    try:
        items: list[dict] = []
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            owner_sql = "AND pm.owner = %s" if owner else ""
            owner_params = [owner] if owner else []
            cur.execute(f"""
                SELECT pm.id AS prospect_id, l.ticker, l.company_name, pm.owner, pm.next_action, pm.next_action_due,
                       pm.opportunity_stage, pm.prospect_score, pm.lead_tier,
                       'overdue_action' AS reason,
                       'Next action "' || COALESCE(pm.next_action, '') || '" was due ' || pm.next_action_due::text AS explanation,
                       pm.next_action_due::timestamptz AS reason_at
                FROM prospect_matrix pm JOIN asx_listings l ON l.id = pm.listing_id
                WHERE pm.next_action_due < CURRENT_DATE AND pm.opportunity_stage NOT IN ('won','lost')
                  AND l.is_active {owner_sql}
            """, owner_params)
            items += [dict(r) for r in cur.fetchall()]

            cur.execute(f"""
                SELECT pm.id AS prospect_id, l.ticker, l.company_name, pm.owner, pm.next_action, pm.next_action_due,
                       pm.opportunity_stage, pm.prospect_score, pm.lead_tier,
                       'new_evidence' AS reason,
                       COUNT(ps.id) || ' new signal(s) in the last 7 days, strongest: ' || MAX(ps.strength::text) AS explanation,
                       MAX(ps.detected_at) AS reason_at
                FROM prospect_matrix pm
                JOIN asx_listings l ON l.id = pm.listing_id
                JOIN pressure_signals ps ON ps.prospect_id = pm.id
                WHERE ps.detected_at > NOW() - INTERVAL '7 days'
                  AND (ps.is_valid IS NULL OR ps.is_valid) AND ps.superseded_by IS NULL
                  AND (pm.is_watchlisted OR pm.opportunity_stage <> 'none' OR pm.prospect_score >= 8)
                  AND pm.status NOT IN ('disqualified','archived') AND l.is_active {owner_sql}
                GROUP BY pm.id, l.id
            """, owner_params)
            items += [dict(r) for r in cur.fetchall()]

            cur.execute(f"""
                SELECT pm.id AS prospect_id, l.ticker, l.company_name, pm.owner, pm.next_action, pm.next_action_due,
                       pm.opportunity_stage, pm.prospect_score, pm.lead_tier,
                       'stale_research' AS reason,
                       'Priority company; last enrichment ' || COALESCE(to_char(MAX(e.completed_at), 'YYYY-MM-DD'), 'never') AS explanation,
                       MAX(e.completed_at) AS reason_at
                FROM prospect_matrix pm
                JOIN asx_listings l ON l.id = pm.listing_id
                LEFT JOIN enrichment_log e ON e.listing_id = l.id AND e.success
                WHERE (pm.is_watchlisted OR pm.opportunity_stage IN ('researching','outreach_planned','contacted','meeting','proposal'))
                  AND pm.status NOT IN ('disqualified','archived') AND l.is_active {owner_sql}
                GROUP BY pm.id, l.id
                HAVING MAX(e.completed_at) IS NULL OR MAX(e.completed_at) < NOW() - INTERVAL '30 days'
            """, owner_params)
            items += [dict(r) for r in cur.fetchall()]

        for row in _dq_review_rows(conn, 50):
            items.append({"prospect_id": row["prospect_id"], "ticker": row["ticker"], "company_name": row["company_name"],
                          "owner": None, "next_action": None, "next_action_due": None, "opportunity_stage": "none",
                          "prospect_score": None, "lead_tier": None, "reason": "dq_review",
                          "explanation": f"Disqualified ({row['dq_reason_code']}) but {row['review_reason']}",
                          "reason_at": row["detail_fetched_at"]})

        order = {"overdue_action": 0, "new_evidence": 1, "stale_research": 2, "dq_review": 3}
        items.sort(key=lambda r: (order.get(r["reason"], 9), -(float(r["prospect_score"] or 0))))
        counts = {}
        for r in items:
            counts[r["reason"]] = counts.get(r["reason"], 0) + 1
        return {"items": items[:limit], "total": len(items), "counts": counts,
                "generated_at": datetime.now(timezone.utc).isoformat()}
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Endpoints: Operations & metrics (Phase 5)
# ---------------------------------------------------------------------------

@app.get("/api/ops/health")
def ops_health():
    """Operator view: freshness, coverage, jobs, alerts, spend, database."""
    from alerts import recent_events
    from ai_budget import month_spend
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                  (SELECT MAX(completed_at) FROM refresh_runs WHERE status = 'completed') AS last_successful_refresh,
                  (SELECT COUNT(*) FROM refresh_runs WHERE status = 'failed' AND started_at > NOW() - INTERVAL '7 days') AS failed_refreshes_7d,
                  (SELECT COUNT(*) FROM refresh_runs WHERE status = 'completed' AND started_at > NOW() - INTERVAL '30 days') AS ok_refreshes_30d,
                  (SELECT COUNT(*) FROM refresh_runs WHERE started_at > NOW() - INTERVAL '30 days') AS all_refreshes_30d,
                  (SELECT COUNT(*) FROM asx_listings WHERE is_active AND is_target_sector) AS target_companies,
                  (SELECT COUNT(*) FROM asx_listings WHERE is_active AND is_target_sector AND detail_fetched_at > NOW() - INTERVAL '36 hours') AS detail_fresh_36h,
                  (SELECT COUNT(*) FROM asx_listings WHERE is_active AND is_target_sector AND (market_cap_aud IS NULL OR market_cap_aud = 0)) AS missing_market_cap,
                  (SELECT COUNT(*) FROM asx_listings WHERE is_active AND is_target_sector AND day_volume IS NULL) AS missing_volume,
                  (SELECT COUNT(DISTINCT prospect_id) FROM pressure_signals) AS companies_with_signals,
                  (SELECT COUNT(*) FROM prospect_matrix pm JOIN asx_listings l ON l.id = pm.listing_id
                     WHERE l.is_active AND pm.is_watchlisted AND NOT EXISTS (
                       SELECT 1 FROM enrichment_log e WHERE e.listing_id = l.id AND e.success AND e.completed_at > NOW() - INTERVAL '30 days')) AS watchlist_stale_research,
                  (SELECT COUNT(*) FROM prospect_contacts WHERE is_active AND email IS NOT NULL
                     AND (last_verified_at IS NULL OR last_verified_at < NOW() - INTERVAL '90 days')) AS contacts_unverified_90d,
                  (SELECT MAX(created_at) FROM ops_events WHERE event_type IN ('backup_completed','restore_rehearsal')) AS last_backup_event,
                  pg_database_size(current_database()) AS db_size_bytes
            """)
            snap = dict(cur.fetchone())
        jobs_stats = jobs.queue_stats(conn)
        target = snap["target_companies"] or 0
        snap["detail_freshness_pct"] = round(100.0 * snap["detail_fresh_36h"] / target, 1) if target else None
        snap["scheduled_success_pct_30d"] = (round(100.0 * snap["ok_refreshes_30d"] / snap["all_refreshes_30d"], 1)
                                             if snap["all_refreshes_30d"] else None)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "version": APP_VERSION, "env": APP_ENV,
            "auth_configured": AUTH_CONFIGURED, "worker_in_process": RUN_WORKER, "scheduler_enabled": SCHEDULER_ENABLED,
            "freshness": snap, "jobs": jobs_stats,
            "ai_spend": month_spend(conn),
            "recent_events": recent_events(conn, 20),
            "targets": {
                "detail_freshness_pct": 95, "scheduled_success_pct_30d": 95,
                "note": "Initial acceptance targets from the roadmap; confirm with the operator.",
            },
        }
    finally:
        put_conn(conn)


@app.get("/api/ops/events")
def ops_events(limit: int = Query(100, ge=1, le=500)):
    from alerts import recent_events
    conn = get_conn()
    try:
        return {"events": recent_events(conn, limit)}
    finally:
        put_conn(conn)


@app.post("/api/ops/alerts/check")
def ops_run_alert_checks():
    from alerts import check_alert_conditions
    conn = get_conn()
    try:
        return {"fired": check_alert_conditions(conn)}
    finally:
        put_conn(conn)


@app.get("/api/metrics/funnel")
def metrics_funnel():
    """Opportunity funnel by stage, sector and cohort month, with lost reasons."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT opportunity_stage, COUNT(*) AS n FROM v_opportunity_funnel GROUP BY 1")
            by_stage = {r["opportunity_stage"]: r["n"] for r in cur.fetchall()}
            cur.execute("""
                SELECT gics_sector, opportunity_stage, COUNT(*) AS n FROM v_opportunity_funnel
                GROUP BY 1, 2 ORDER BY 1, 2
            """)
            by_sector = [dict(r) for r in cur.fetchall()]
            cur.execute("""
                SELECT cohort_month,
                       COUNT(*) AS opportunities,
                       COUNT(*) FILTER (WHERE first_contacted_at IS NOT NULL) AS contacted,
                       COUNT(*) FILTER (WHERE first_meeting_at IS NOT NULL) AS meetings,
                       COUNT(*) FILTER (WHERE first_proposal_at IS NOT NULL) AS proposals,
                       COUNT(*) FILTER (WHERE opportunity_stage = 'won') AS won,
                       COUNT(*) FILTER (WHERE opportunity_stage = 'lost') AS lost,
                       ROUND(AVG(EXTRACT(EPOCH FROM (first_meeting_at - first_contacted_at)) / 86400)::numeric, 1) AS avg_days_contact_to_meeting
                FROM v_opportunity_funnel GROUP BY 1 ORDER BY 1 DESC
            """)
            cohorts = [dict(r) for r in cur.fetchall()]
            cur.execute("""
                SELECT COALESCE(outcome_reason, '(no reason recorded)') AS reason, COUNT(*) AS n
                FROM v_opportunity_funnel WHERE opportunity_stage = 'lost' GROUP BY 1 ORDER BY 2 DESC
            """)
            lost = [dict(r) for r in cur.fetchall()]
            cur.execute("""
                SELECT score_policy_version, opportunity_stage, COUNT(*) AS n FROM v_opportunity_funnel
                GROUP BY 1, 2 ORDER BY 1, 2
            """)
            by_policy = [dict(r) for r in cur.fetchall()]
        total = sum(by_stage.values())
        return {"by_stage": by_stage, "by_sector": by_sector, "cohorts": cohorts, "lost_reasons": lost,
                "by_score_policy": by_policy, "sample_size": total,
                "caveat": "Small samples. Conversion is descriptive, not evidence of causation; report lag and n alongside rates."}
    finally:
        put_conn(conn)


@app.get("/api/metrics/costs")
def metrics_costs(months: int = Query(6, ge=1, le=36)):
    """Unit economics: provider spend per analysed company and per opportunity."""
    from ai_budget import ledger_summary, month_spend
    conn = get_conn()
    try:
        rows = ledger_summary(conn, months)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT to_char(stage_changed_at, 'YYYY-MM') AS period,
                       COUNT(*) FILTER (WHERE opportunity_stage IN ('meeting','proposal','won')) AS qualified_conversations
                FROM prospect_matrix WHERE opportunity_stage <> 'none' GROUP BY 1
            """)
            convos = {r["period"]: r["qualified_conversations"] for r in cur.fetchall()}
        for r in rows:
            spend = float(r["spend_usd"] or 0)
            r["cost_per_analysed_company_usd"] = round(spend / r["companies"], 4) if r["companies"] else None
            q = convos.get(r["period"], 0)
            r["qualified_conversations"] = q
            r["cost_per_qualified_conversation_usd"] = round(spend / q, 4) if q else None
        return {"months": rows, "current": month_spend(conn),
                "caveat": "Estimated from token counts × list prices; reconcile with the provider invoice before relying on it."}
    finally:
        put_conn(conn)


@app.get("/api/health/live")
def health_live():
    """Liveness: the process is up. Never touches the database."""
    return {"status": "alive", "version": APP_VERSION, "env": APP_ENV,
            "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/health")
@app.get("/api/health/ready")
def health_check():
    """Readiness: database reachable + migrations current. 503 (sanitised) when not."""
    body = {
        "status": "healthy", "version": APP_VERSION, "env": APP_ENV,
        "auth_configured": AUTH_CONFIGURED, "worker_in_process": RUN_WORKER,
        "scheduler_enabled": SCHEDULER_ENABLED,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.execute("SELECT COUNT(*) FROM schema_migrations")
            body["migrations_applied"] = cur.fetchone()[0]
        pending = migrations.pending(conn)
        body["migrations_pending"] = len(pending)
        if pending:
            body["status"] = "degraded"
        return body
    except Exception as exc:
        logger.error("readiness check failed: %s", exc)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return JSONResponse(status_code=503, content={**body, "status": "unhealthy",
                                                      "detail": "database unavailable"})
    finally:
        if conn is not None:
            put_conn(conn)


# ---------------------------------------------------------------------------
# Static File Serving (built React frontend)
# ---------------------------------------------------------------------------
# After `npm run build` in frontend/, mount the dist/ folder so FastAPI
# serves the SPA. In dev, Vite proxies /api to here instead.

if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="static-assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        """Serve the React SPA — all non-API routes fall through here."""
        file_path = FRONTEND_DIST / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIST / "index.html")
