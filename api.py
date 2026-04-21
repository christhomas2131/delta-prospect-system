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
    POST /api/refresh                - Trigger manual data refresh
    GET  /api/refresh/history        - Recent refresh runs
    POST /api/enrich/{ticker}        - Trigger single company enrichment
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
from datetime import datetime, timezone
from typing import Callable, Optional
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import csv
import io

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool

# ---------------------------------------------------------------------------
# In-memory API key store (not persisted to DB — intentional)
# ---------------------------------------------------------------------------

_api_key_store: dict = {"key": None, "valid": False, "source": None}

# ---------------------------------------------------------------------------
# In-memory enrichment progress tracker
# ---------------------------------------------------------------------------

_enrich_progress: dict = {
    "running": False, "current": 0, "total": 0, "ticker": "", "ok": 0, "skip": 0, "fail": 0,
    # AI deep analysis phase (runs after main enrichment loop when API key is set)
    "ai_running": False, "ai_current": 0, "ai_total": 0, "ai_ticker": "",
    "ai_ok": 0, "ai_fail": 0, "ai_skip": 0,
    "ai_status": "idle", "ai_message": "", "ai_selection_basis": "prospect_score",
}


def _reset_ai_progress():
    """Reset the AI deep analysis phase state before each batch run."""
    _enrich_progress["ai_running"] = False
    _enrich_progress["ai_current"] = 0
    _enrich_progress["ai_total"] = 0
    _enrich_progress["ai_ticker"] = ""
    _enrich_progress["ai_ok"] = 0
    _enrich_progress["ai_fail"] = 0
    _enrich_progress["ai_skip"] = 0
    _enrich_progress["ai_status"] = "idle"
    _enrich_progress["ai_message"] = ""
    _enrich_progress["ai_selection_basis"] = "prospect_score"

# ---------------------------------------------------------------------------
# In-memory refresh progress tracker
# ---------------------------------------------------------------------------

_refresh_progress: dict = {"running": False, "phase": "", "detail": ""}
_deep_analysis_jobs: dict = {}
_deep_analysis_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _parse_database_url(url: str) -> dict:
    """Parse a DATABASE_URL (postgresql://user:pass@host:port/dbname) into psycopg2 params."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "dbname": (parsed.path or "/delta_prospect").lstrip("/"),
        "user": parsed.username or "delta",
        "password": parsed.password or "delta_dev",
    }

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    DB_CONFIG = _parse_database_url(DATABASE_URL)
else:
    DB_CONFIG = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "delta_prospect"),
        "user": os.getenv("DB_USER", "delta"),
        "password": os.getenv("DB_PASSWORD", "delta_dev"),
    }

PORT = int(os.getenv("PORT", "8000"))

# Auth (optional — set both to enable)
AUTH_USER = os.getenv("AUTH_USER")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD")

# CORS (comma-separated origins, or * for dev)
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

# Cron secret (protects /api/cron/enrich-all)
CRON_SECRET = os.getenv("CRON_SECRET", "")

# Static files (built frontend)
FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")

# ---------------------------------------------------------------------------
# Database Pool
# ---------------------------------------------------------------------------

db_pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    # Startup validation — fail fast with clear messages
    try:
        db_pool = pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            **DB_CONFIG,
        )
    except psycopg2.OperationalError as e:
        logger.error(
            "Cannot connect to database at %s:%s/%s as user '%s'. "
            "Check DATABASE_URL or DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD. "
            "Error: %s",
            DB_CONFIG["host"], DB_CONFIG["port"], DB_CONFIG["dbname"],
            DB_CONFIG["user"], e,
        )
        sys.exit(1)

    # Always run schema.sql on startup — it's fully idempotent and ensures
    # any new columns, functions, or migrations are applied.
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            schema_path = Path(__file__).parent / "schema.sql"
            if schema_path.is_file():
                logger.info("Running schema.sql (idempotent — safe to re-run)...")
                sql = schema_path.read_text(encoding="utf-8")
                cur.execute(sql)
                conn.commit()
                logger.info("Schema up to date")
            else:
                logger.error("schema.sql not found at %s — cannot initialize database", schema_path)
                sys.exit(1)
    finally:
        db_pool.putconn(conn)

    logger.info("Database pool initialized — connection verified")

    # Auto-load Anthropic API key from environment variable if set
    env_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if env_key and env_key.startswith("sk-ant-"):
        _api_key_store["key"] = env_key
        _api_key_store["valid"] = True
        _api_key_store["source"] = "env"
        logger.info("Anthropic API key loaded from ANTHROPIC_API_KEY environment variable")

    yield
    if db_pool:
        db_pool.closeall()
        logger.info("Database pool closed")


def get_conn():
    return db_pool.getconn()

def put_conn(conn):
    db_pool.putconn(conn)

# ---------------------------------------------------------------------------
# App Setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Delta Prospect System API",
    version="2.0.0",
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


# ---------------------------------------------------------------------------
# Basic Auth (optional — only active when AUTH_USER + AUTH_PASSWORD are set)
# ---------------------------------------------------------------------------

import base64
import secrets

def check_auth(request: Request):
    """Verify Basic Auth if AUTH_USER and AUTH_PASSWORD are configured."""
    if not AUTH_USER or not AUTH_PASSWORD:
        return  # Auth not configured — allow all
    if request.url.path == "/api/health":
        return  # Health check is always public

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

if AUTH_USER and AUTH_PASSWORD:
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.url.path == "/api/health":
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
    logger.info("Basic auth enabled (AUTH_USER=%s)", AUTH_USER)
else:
    logger.info("Basic auth disabled — set AUTH_USER + AUTH_PASSWORD to enable")

# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

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


class RefreshRequest(BaseModel):
    triggered_by: str = "manual"


class ApiKeyRequest(BaseModel):
    api_key: str


class V3CollectRequest(BaseModel):
    max_documents: int = Field(default=6, ge=1, le=20)
    force_refresh: bool = False

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
                "lead_tier", "size_of_prize",
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
                              ps2.detected_at DESC LIMIT 1) AS top_signal_url
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
            }
    finally:
        put_conn(conn)


@app.patch("/api/prospects/{prospect_id}")
def update_prospect(prospect_id: str, update: ProspectUpdate):
    """Update a prospect's status, notes, or buyer info."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Build dynamic update
            set_clauses = []
            params = []
            
            update_dict = update.model_dump(exclude_none=True)
            if not update_dict:
                raise HTTPException(status_code=400, detail="No fields to update")
            
            # Validate status if provided
            valid_statuses = {
                "unscreened", "qualified", "enriched", "ready_for_outreach",
                "suggested_dq", "disqualified", "archived"
            }
            if update.status and update.status not in valid_statuses:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Invalid status. Must be one of: {valid_statuses}"
                )
            
            for field_name, value in update_dict.items():
                if field_name == "status":
                    set_clauses.append(f"{field_name} = %s::prospect_status")
                else:
                    set_clauses.append(f"{field_name} = %s")
                params.append(value)
            
            set_clauses.append("status_changed_by = 'api'")
            
            params.append(prospect_id)
            
            cur.execute(f"""
                UPDATE prospect_matrix
                SET {', '.join(set_clauses)}
                WHERE id = %s
                RETURNING id, status
            """, params)
            
            result = cur.fetchone()
            if not result:
                raise HTTPException(status_code=404, detail="Prospect not found")
            
            conn.commit()
            return {"message": "Updated", "prospect_id": result["id"], "status": result["status"]}
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
):
    """Run deep analysis for one prospect and return the API response payload."""
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
        )

        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])

        if progress_callback:
            progress_callback(90, "saving_results", "Saving analysis results")
        inserted = _store_deep_analysis_result(
            conn,
            prospect_id=prospect_id,
            listing_id=str(prospect["listing_id"]),
            ticker=prospect["ticker"],
            existing_signals=existing_signals,
            result=result,
        )

        if result.get("analysis_mode") == "full_documents":
            try:
                from v3_intelligence import store_intelligence_run
                store_intelligence_run(
                    conn,
                    prospect_id=prospect_id,
                    listing_id=str(prospect["listing_id"]),
                    result=result,
                )
            except Exception as exc:
                logger.warning("Failed to store V3 intelligence run for %s: %s", prospect["ticker"], exc)

        tokens_used = result.get("tokens_used", 0)
        validated = result.get("validated_signals", [])
        profile = result.get("refined_profile", {})

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
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Deep analysis failed for %s", prospect_id)
        raise HTTPException(status_code=500, detail=f"Deep analysis error: {e}")
    finally:
        put_conn(conn)


def _run_deep_analysis_job(prospect_id: str, job_id: str):
    progress_callback = _progress_updater(prospect_id, job_id)
    try:
        result = _execute_deep_analysis(prospect_id, progress_callback=progress_callback)
        _update_deep_analysis_job(
            prospect_id,
            job_id=job_id,
            status="completed",
            progress_pct=100,
            stage="completed",
            message="Deep analysis complete",
            result=result,
            completed_at=_now_iso(),
            error=None,
        )
    except HTTPException as exc:
        _update_deep_analysis_job(
            prospect_id,
            job_id=job_id,
            status="failed",
            progress_pct=100,
            stage="failed",
            message=str(exc.detail),
            error=str(exc.detail),
            completed_at=_now_iso(),
        )
    except Exception as exc:
        logger.exception("Background deep analysis failed for %s", prospect_id)
        _update_deep_analysis_job(
            prospect_id,
            job_id=job_id,
            status="failed",
            progress_pct=100,
            stage="failed",
            message=f"Deep analysis error: {exc}",
            error=f"Deep analysis error: {exc}",
            completed_at=_now_iso(),
        )


@app.post("/api/prospects/{prospect_id}/deep-analysis")
def deep_analysis(prospect_id: str):
    """Run Claude deep analysis for a single prospect. Synchronous (~5-10s)."""
    return _execute_deep_analysis(prospect_id)


@app.post("/api/prospects/{prospect_id}/deep-analysis/start")
def start_deep_analysis(prospect_id: str):
    """Start deep analysis in the background for one prospect."""
    current = _get_deep_analysis_job(prospect_id)
    if current and current.get("status") == "running":
        return current

    if not _api_key_store["valid"]:
        raise HTTPException(
            status_code=402,
            detail="No valid API key configured. Go to Settings to add your Anthropic API key.",
        )

    job_id = str(uuid.uuid4())
    started = _update_deep_analysis_job(
        prospect_id,
        job_id=job_id,
        status="running",
        progress_pct=2,
        stage="queued",
        message="Queued deep analysis",
        started_at=_now_iso(),
        completed_at=None,
        error=None,
        result=None,
    )
    thread = threading.Thread(
        target=_run_deep_analysis_job,
        args=(prospect_id, job_id),
        daemon=True,
    )
    thread.start()
    return started


@app.get("/api/prospects/{prospect_id}/deep-analysis/status")
def get_deep_analysis_status(prospect_id: str):
    """Get the latest deep analysis background job status for one prospect."""
    current = _get_deep_analysis_job(prospect_id)
    if not current:
        return {
            "prospect_id": prospect_id,
            "status": "idle",
            "progress_pct": 0,
            "stage": "idle",
            "message": "No deep analysis running",
            "updated_at": _now_iso(),
        }
    return current


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

def _run_refresh_with_progress(triggered_by: str):
    """Wrapper around ASX refresh that tracks progress and logs to refresh_runs."""
    _refresh_progress["running"] = True
    _refresh_progress["phase"] = "Fetching ASX CSV..."
    _refresh_progress["detail"] = ""
    run_id = None

    try:
        from asx_scraper import (
            fetch_asx_csv,
            parse_asx_csv,
            upsert_listings,
            backfill_prospect_matrix,
            refresh_target_company_details,
        )
        import httpx

        # Log a "running" row
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO refresh_runs (run_type, status, triggered_by) "
                    "VALUES ('manual', 'running', %s) RETURNING id",
                    (triggered_by,),
                )
                run_id = str(cur.fetchone()[0])
                conn.commit()
        finally:
            put_conn(conn)

        _refresh_progress["phase"] = "Downloading ASX listings..."
        with httpx.Client(headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-AU,en;q=0.9",
        }, timeout=30) as client:
            csv_text = fetch_asx_csv(client)

        _refresh_progress["phase"] = "Parsing listings..."
        listings = parse_asx_csv(csv_text)
        _refresh_progress["detail"] = f"{len(listings)} companies found"

        _refresh_progress["phase"] = "Updating database..."
        from asx_scraper import get_conn as scraper_conn
        sconn = scraper_conn()
        try:
            stats = upsert_listings(sconn, listings)
            _refresh_progress["detail"] = f"{stats.new_listings} new, {stats.updated_listings} updated, {stats.delisted_count} delisted"

            _refresh_progress["phase"] = "Backfilling prospect matrix..."
            backfill_prospect_matrix(sconn)
            _refresh_progress["phase"] = "Fetching company locations..."
            _refresh_progress["detail"] = "Pulling registered office address data from ASX company profiles"
            refresh_target_company_details(sconn, only_missing_location=True, triggered_by=triggered_by)
            sconn.commit()
        finally:
            sconn.close()

        # Update the run row with results
        conn = get_conn()
        try:
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
        finally:
            put_conn(conn)

        _refresh_progress["phase"] = "Complete"
        _refresh_progress["detail"] = f"{len(listings)} listings — {stats.new_listings} new, {stats.target_sector_count} target sector"
        logger.info(f"Refresh complete: {len(listings)} listings")
    except Exception as e:
        logger.error(f"Refresh failed: {e}")
        _refresh_progress["phase"] = "Failed"
        _refresh_progress["detail"] = str(e)
        # Log failure
        if run_id:
            try:
                conn = get_conn()
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE refresh_runs SET status = 'failed', completed_at = NOW(), error_message = %s WHERE id = %s",
                        (str(e)[:500], run_id),
                    )
                    conn.commit()
                put_conn(conn)
            except Exception:
                pass
    finally:
        _refresh_progress["running"] = False


@app.post("/api/refresh")
def trigger_refresh(req: RefreshRequest, background_tasks: BackgroundTasks):
    """Trigger a manual ASX data refresh (runs in background)."""
    if _refresh_progress["running"]:
        return {"message": "Refresh already running", "running": True}

    background_tasks.add_task(_run_refresh_with_progress, req.triggered_by)
    return {"message": "Refresh started", "triggered_by": req.triggered_by}


@app.get("/api/refresh/status")
def get_refresh_status():
    """Return current refresh progress."""
    return {
        "running": _refresh_progress["running"],
        "phase": _refresh_progress["phase"],
        "detail": _refresh_progress["detail"],
    }


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
            return row or {}
    finally:
        put_conn(conn)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_deep_analysis_job(prospect_id: str) -> Optional[dict]:
    with _deep_analysis_lock:
        job = _deep_analysis_jobs.get(prospect_id)
        return dict(job) if job else None


def _update_deep_analysis_job(prospect_id: str, **updates) -> dict:
    with _deep_analysis_lock:
        current = dict(_deep_analysis_jobs.get(prospect_id) or {})
        current.update(updates)
        current["prospect_id"] = prospect_id
        current["updated_at"] = _now_iso()
        _deep_analysis_jobs[prospect_id] = current
        return dict(current)


def _progress_updater(prospect_id: str, job_id: str) -> Callable[..., None]:
    def _update(progress_pct: int, stage: str, message: str, **extra):
        current = _get_deep_analysis_job(prospect_id)
        if not current or current.get("job_id") != job_id:
            return
        payload = {
            "status": "running",
            "progress_pct": max(0, min(int(progress_pct), 99)),
            "stage": stage,
            "message": message,
        }
        payload.update(extra)
        _update_deep_analysis_job(prospect_id, **payload)

    return _update


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
):
    """
    Prefer the V3 full-document path when Firecrawl is configured and documents
    can be collected. Fall back to the legacy headline-only analysis otherwise.
    """
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
                                ticker: str, existing_signals: list, result: dict) -> int:
    """
    Persist deep analysis output to the database.
    Shared by the manual endpoint and the auto-batch runner.
    Returns the number of new AI signals inserted.
    """
    import json as _json
    from prize_calculator import calculate_size_of_prize

    valid_pt = {"production","license_to_operate","cost","people","quality","future_readiness"}
    valid_st = {"weak","moderate","strong"}
    agent_version = result.get("analysis_version") or "claude-deep-v1"
    inserted = 0

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
                    SET is_valid = %s, validated_by = %s,
                        validated_at = NOW(), confidence_score = %s
                    WHERE id = %s
                """, (v["confirmed"], agent_version, new_conf, sig["id"]))

        # Insert new AI-detected signals
        for ns in result.get("new_signals", []):
            pt = (ns.get("pressure_type") or "").lower()
            st = (ns.get("strength") or "moderate").lower()
            if pt not in valid_pt or st not in valid_st:
                continue
            source_url = (
                f"claude-deep://{ticker}/{pt}/"
                f"{abs(hash(ns.get('summary','') + str(time.time())))}"
            )
            try:
                cur.execute("""
                    INSERT INTO pressure_signals
                        (prospect_id, pressure_type, strength, summary,
                         source_type, source_url, source_title,
                         confidence_score, model_version, extracted_quote, is_valid)
                    VALUES (%s,%s,%s,%s,'asx_announcement',%s,%s,
                            0.75,%s,%s,TRUE)
                    ON CONFLICT (prospect_id, pressure_type, source_url) DO NOTHING
                """, (prospect_id, pt, st, ns.get("summary",""),
                      source_url, ns.get("source_title",""), agent_version, ns.get("reasoning","")))
                inserted += cur.rowcount
            except Exception as exc:
                logger.error("Failed to insert AI signal for %s: %s", ticker, exc)

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

        # Recalculate prospect score
        cur.execute("SELECT calculate_prospect_score(%s)", (prospect_id,))

        # Recalculate prize now that new AI signals may have been added
        try:
            prize = calculate_size_of_prize(conn, prospect_id)
            cur.execute("""
                UPDATE prospect_matrix SET size_of_prize = %s, prize_breakdown = %s
                WHERE id = %s
            """, (prize["total_prize"], _json.dumps(prize), prospect_id))
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

    logger.info("%s: deep analysis stored — %d new signals, %d tokens", ticker, inserted, tokens_used)
    return inserted


def _auto_deep_analysis(conn):
    """
    Run Deep Analysis on the top 10 prospects by prospect score.
    Called automatically after Enrich All if an Anthropic API key is configured.
    Updates _enrich_progress with ai_* fields so the frontend can show progress.
    """
    import time as _time

    _enrich_progress["ai_selection_basis"] = "prospect_score"

    if not _api_key_store.get("valid") or not _api_key_store.get("key"):
        _enrich_progress["ai_status"] = "skipped_no_api_key"
        _enrich_progress["ai_message"] = "AI deep analysis skipped: no valid Anthropic API key configured."
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
            GROUP BY pm.id, pm.listing_id, pm.size_of_prize, pm.prize_breakdown,
                     pm.prospect_score, l.ticker, l.company_name, l.gics_sector
            HAVING COUNT(ps.id) > 0
            ORDER BY pm.prospect_score DESC NULLS LAST, signal_count DESC NULLS LAST
            LIMIT 10
        """)
        top_10 = cur.fetchall()

    if not top_10:
        _enrich_progress["ai_status"] = "skipped_no_candidates"
        _enrich_progress["ai_message"] = "AI deep analysis skipped: no scored prospects with signals were available."
        return

    _enrich_progress["ai_running"] = True
    _enrich_progress["ai_total"] = len(top_10)
    _enrich_progress["ai_current"] = 0
    _enrich_progress["ai_ticker"] = ""
    _enrich_progress["ai_status"] = "running"
    _enrich_progress["ai_message"] = f"Running AI deep analysis on top {len(top_10)} prospects by score."
    logger.info("Auto deep analysis starting on %d top prospects by score", len(top_10))

    for i, p in enumerate(top_10):
        tk = p["ticker"]
        prospect_id = str(p["prospect_id"])
        listing_id = str(p["listing_id"])

        _enrich_progress["ai_current"] = i + 1
        _enrich_progress["ai_ticker"] = tk

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
            )

            if "error" in result:
                _enrich_progress["ai_fail"] += 1
                _enrich_progress["ai_message"] = f"AI analysis failed for {tk}: {result['error']}"
                logger.warning("Auto deep analysis error for %s: %s", tk, result["error"])
            else:
                _store_deep_analysis_result(
                    conn, prospect_id, listing_id, tk, existing_signals, result
                )
                if result.get("analysis_mode") == "full_documents":
                    try:
                        from v3_intelligence import store_intelligence_run
                        store_intelligence_run(conn, prospect_id, listing_id, result)
                    except Exception as exc:
                        logger.warning("Failed to store V3 intelligence run for %s: %s", tk, exc)
                _enrich_progress["ai_ok"] += 1
                _enrich_progress["ai_message"] = f"AI analysis stored for {tk}"
        except Exception as exc:
            _enrich_progress["ai_fail"] += 1
            _enrich_progress["ai_message"] = f"AI analysis failed for {tk}: {exc}"
            logger.error("Auto deep analysis failed for %s: %s", tk, exc)

        if i < len(top_10) - 1:
            _time.sleep(2.0)

    _enrich_progress["ai_running"] = False
    _enrich_progress["ai_ticker"] = ""
    _enrich_progress["ai_status"] = "completed_with_errors" if _enrich_progress["ai_fail"] else "completed"
    _enrich_progress["ai_message"] = (
        f"AI deep analysis complete: {_enrich_progress['ai_ok']} succeeded, "
        f"{_enrich_progress['ai_fail']} failed."
    )
    logger.info("Auto deep analysis complete")


def _run_batch_with_progress():
    """Wrapper around enrichment run_batch that tracks progress.
    Processes ALL target sector companies, not just unscreened/qualified."""
    from enrichment_agent import get_conn as enrich_conn, detect_signals, generate_profile, save_results
    from asx_browser import ASXFetcher
    import time as _time

    _enrich_progress["running"] = True
    _enrich_progress["current"] = 0
    _enrich_progress["ok"] = 0
    _enrich_progress["skip"] = 0
    _enrich_progress["fail"] = 0
    _enrich_progress["ticker"] = ""
    _reset_ai_progress()

    conn = enrich_conn()
    try:
        # Fetch ALL prospects (not just unscreened) so re-enrichment works
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT pm.id prospect_id, pm.status, l.id listing_id,
                       l.ticker, l.company_name, l.gics_sector,
                       l.principal_activities
                FROM prospect_matrix pm JOIN asx_listings l ON l.id=pm.listing_id
                WHERE l.is_active=TRUE AND l.is_target_sector=TRUE
                ORDER BY l.market_cap_aud DESC NULLS LAST
            """)
            prospects = cur.fetchall()
        _enrich_progress["total"] = len(prospects)
        logger.info(f"Batch enrichment started: {len(prospects)} prospects")

        with ASXFetcher() as fetcher:
            for i, p in enumerate(prospects):
                tk = p["ticker"]
                _enrich_progress["current"] = i + 1
                _enrich_progress["ticker"] = tk
                try:
                    anns = fetcher.fetch_announcements(tk)
                    _time.sleep(2.0)
                    if not anns:
                        _enrich_progress["skip"] += 1
                        continue
                    signals = detect_signals(p["company_name"], anns)
                    profile = generate_profile(p["gics_sector"], signals)
                    save_results(conn, p, signals, profile, anns, p.get("principal_activities") or "")
                    _enrich_progress["ok"] += 1
                except Exception as e:
                    logger.error(f"{tk}: {e}")
                    _enrich_progress["fail"] += 1
        # Auto deep analysis on top 10 by signal count (if API key is set)
        _auto_deep_analysis(conn)

    except Exception as e:
        logger.error(f"Batch enrichment failed: {e}")
    finally:
        conn.close()
        _enrich_progress["running"] = False
        _enrich_progress["ai_running"] = False
        logger.info(f"Batch enrichment finished: {_enrich_progress['ok']} ok, {_enrich_progress['skip']} skip, {_enrich_progress['fail']} fail")


@app.post("/api/enrich/batch")
def trigger_batch_enrichment(background_tasks: BackgroundTasks):
    """Trigger batch enrichment for ALL target sector prospects (runs in background)."""
    if _enrich_progress["running"]:
        return {"message": "Enrichment already running", "count": _enrich_progress["total"], "running": True}

    # Count all target sector prospects
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM prospect_matrix pm "
                "JOIN asx_listings l ON l.id = pm.listing_id "
                "WHERE l.is_active = TRUE AND l.is_target_sector = TRUE"
            )
            count = cur.fetchone()[0]
    finally:
        put_conn(conn)

    if count == 0:
        return {"message": "No target sector prospects to enrich", "count": 0}

    background_tasks.add_task(_run_batch_with_progress)
    return {"message": f"Enrichment started for {count} companies", "count": count}


@app.get("/api/enrich/status")
def get_enrichment_status():
    """Return current enrichment progress, including AI deep analysis phase."""
    return {
        "running":    _enrich_progress["running"],
        "current":    _enrich_progress["current"],
        "total":      _enrich_progress["total"],
        "ticker":     _enrich_progress["ticker"],
        "ok":         _enrich_progress["ok"],
        "skip":       _enrich_progress["skip"],
        "fail":       _enrich_progress["fail"],
        "ai_running": _enrich_progress["ai_running"],
        "ai_current": _enrich_progress["ai_current"],
        "ai_total":   _enrich_progress["ai_total"],
        "ai_ticker":  _enrich_progress["ai_ticker"],
        "ai_ok":      _enrich_progress["ai_ok"],
        "ai_fail":    _enrich_progress["ai_fail"],
        "ai_skip":    _enrich_progress["ai_skip"],
        "ai_status":  _enrich_progress["ai_status"],
        "ai_message": _enrich_progress["ai_message"],
        "ai_selection_basis": _enrich_progress["ai_selection_basis"],
    }


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
def trigger_enrichment(ticker: str, background_tasks: BackgroundTasks):
    """Trigger enrichment for a single company (runs in background)."""
    from enrichment_agent import run_single

    background_tasks.add_task(run_single, ticker.upper())
    return {"message": f"Enrichment started for {ticker.upper()}"}


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
    result = {"scrape": None, "enrichment": None, "errors": []}

    # Step 1: Full ASX data refresh
    try:
        from asx_scraper import run_full_refresh
        stats = run_full_refresh(triggered_by="cron")
        result["scrape"] = {
            "total_parsed": stats.total_parsed,
            "new_listings": stats.new_listings,
            "updated_listings": stats.updated_listings,
            "delisted_count": stats.delisted_count,
            "target_sector_count": stats.target_sector_count,
        }
    except Exception as e:
        logger.error("Cron scrape failed: %s", e)
        result["errors"].append(f"Scrape failed: {e}")

    # Step 2: Batch enrichment
    try:
        from enrichment_agent import run_batch
        run_batch()
        result["enrichment"] = {"status": "completed"}
    except Exception as e:
        logger.error("Cron enrichment failed: %s", e)
        result["errors"].append(f"Enrichment failed: {e}")

    status_msg = "completed with errors" if result["errors"] else "completed successfully"
    result["status"] = status_msg
    logger.info("Cron enrich-all %s", status_msg)
    return result


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
# Health Check
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health_check():
    """Basic health check."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}
    finally:
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
