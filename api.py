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
from datetime import datetime, timezone
from typing import Optional
from contextlib import asynccontextmanager
from pathlib import Path

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

_enrich_progress: dict = {"running": False, "current": 0, "total": 0, "ticker": "", "ok": 0, "skip": 0, "fail": 0}

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
    min_strong_signals: Optional[int] = Query(None),
    has_signals: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    watchlist: Optional[bool] = Query(None),
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
                "lead_tier",
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
                              ps2.detected_at DESC LIMIT 1) AS top_signal
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
                    pm.analyst_notes
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

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Ticker", "Company Name", "Sector", "Industry Group", "Market Cap",
            "Lead Tier", "Production", "License to Operate", "Cost", "People",
            "Quality", "Future Readiness", "Total Signals", "Top Signal",
            "Source Announcement", "Source URL", "Latest Signal Date",
            "Status", "Prospect Score", "Analyst Notes",
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
                WHERE listing_id = %s AND agent_version = 'claude-deep-v1'
                ORDER BY completed_at DESC LIMIT 1
            """, (prospect["listing_id"],))
            da_row = cur.fetchone()
            last_deep_analysis_at = da_row["completed_at"] if da_row else None

            return {
                "prospect": prospect,
                "signals": signals,
                "enrichment_history": enrichment_history,
                "deep_analysis_available": _api_key_store["valid"],
                "last_deep_analysis_at": last_deep_analysis_at,
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

@app.post("/api/prospects/{prospect_id}/deep-analysis")
def deep_analysis(prospect_id: str):
    """Run Claude deep analysis for a single prospect. Synchronous (~5-10s)."""
    if not _api_key_store["valid"]:
        raise HTTPException(
            status_code=402,
            detail="No valid API key configured. Go to Settings to add your Anthropic API key.",
        )

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT pm.id, pm.listing_id, l.ticker, l.company_name, l.gics_sector
                FROM prospect_matrix pm
                JOIN asx_listings l ON l.id = pm.listing_id
                WHERE pm.id = %s
            """, (prospect_id,))
            prospect = cur.fetchone()
            if not prospect:
                raise HTTPException(status_code=404, detail="Prospect not found")

            cur.execute("""
                SELECT id, pressure_type, strength, summary, source_url, source_title, model_version
                FROM pressure_signals WHERE prospect_id = %s
                ORDER BY detected_at
            """, (prospect_id,))
            existing_signals = list(cur.fetchall())

        from deep_analysis import run_deep_analysis as _run_deep
        result = _run_deep(
            prospect_id=prospect_id,
            ticker=prospect["ticker"],
            company_name=prospect["company_name"],
            sector=prospect["gics_sector"],
            existing_signals=existing_signals,
            api_key=_api_key_store["key"],
        )

        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Validate/invalidate existing signals
            validated = result.get("validated_signals", [])
            for v in validated:
                idx = v.get("index", -1)
                if 0 <= idx < len(existing_signals):
                    sig = existing_signals[idx]
                    new_confidence = 0.95 if v["confirmed"] else 0.20
                    cur.execute("""
                        UPDATE pressure_signals
                        SET is_valid = %s,
                            validated_by = 'claude-deep-v1',
                            validated_at = NOW(),
                            confidence_score = %s
                        WHERE id = %s
                    """, (v["confirmed"], new_confidence, sig["id"]))

            # Insert new AI-detected signals
            new_signals = result.get("new_signals", [])
            valid_pt = {"production","license_to_operate","cost","people","quality","future_readiness"}
            valid_st = {"weak","moderate","strong"}
            inserted = 0
            for ns in new_signals:
                pt = (ns.get("pressure_type") or "").lower()
                st = (ns.get("strength") or "moderate").lower()
                if pt not in valid_pt or st not in valid_st:
                    continue
                # Synthetic unique URL so the unique constraint works
                source_url = (
                    f"claude-deep://{prospect['ticker']}/{pt}/"
                    f"{abs(hash(ns.get('summary','') + str(time.time())))}"
                )
                try:
                    cur.execute("""
                        INSERT INTO pressure_signals
                            (prospect_id, pressure_type, strength, summary,
                             source_type, source_url, source_title,
                             confidence_score, model_version, extracted_quote, is_valid)
                        VALUES (%s,%s,%s,%s,'asx_announcement',%s,%s,
                                0.75,'claude-deep-v1',%s,TRUE)
                        ON CONFLICT (prospect_id, pressure_type, source_url) DO NOTHING
                    """, (
                        prospect_id, pt, st,
                        ns.get("summary", ""),
                        source_url,
                        ns.get("source_title", ""),
                        ns.get("reasoning", ""),
                    ))
                    inserted += cur.rowcount
                except Exception as exc:
                    logger.error("Failed to insert AI signal: %s", exc)

            # Update strategic profile
            profile = result.get("refined_profile", {})
            if profile:
                lk = max(1, min(10, int(profile.get("likelihood_score") or 5)))
                cur.execute("""
                    UPDATE prospect_matrix SET
                        strategic_direction = COALESCE(%s, strategic_direction),
                        primary_tailwind    = COALESCE(%s, primary_tailwind),
                        primary_headwind    = COALESCE(%s, primary_headwind),
                        likelihood_score    = %s
                    WHERE id = %s
                """, (
                    profile.get("strategic_direction"),
                    profile.get("primary_tailwind"),
                    profile.get("primary_headwind"),
                    lk, prospect_id,
                ))

            # Recalculate score
            cur.execute("SELECT calculate_prospect_score(%s)", (prospect_id,))
            new_score = cur.fetchone()[0]

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
                        'api',NOW(),'claude-deep-v1',%s)
            """, (prospect["listing_id"], len(anns), confirmed_count + inserted, tokens_used))

            conn.commit()

        return {
            "prospect_id": prospect_id,
            "ticker": prospect["ticker"],
            "tokens_used": tokens_used,
            "validated_count": len(validated),
            "confirmed_count": sum(1 for v in validated if v.get("confirmed")),
            "disputed_count": sum(1 for v in validated if not v.get("confirmed")),
            "new_signals_count": inserted,
            "new_score": float(new_score or 0),
            "profile": profile,
            "validated_signals": validated,
        }
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

@app.post("/api/refresh")
def trigger_refresh(req: RefreshRequest, background_tasks: BackgroundTasks):
    """Trigger a manual ASX data refresh (runs in background)."""
    from asx_scraper import run_full_refresh
    
    background_tasks.add_task(run_full_refresh, req.triggered_by)
    return {"message": "Refresh started", "triggered_by": req.triggered_by}


@app.post("/api/enrich/{ticker}")
def trigger_enrichment(ticker: str, background_tasks: BackgroundTasks):
    """Trigger enrichment for a single company (runs in background)."""
    from enrichment_agent import run_single

    background_tasks.add_task(run_single, ticker.upper())
    return {"message": f"Enrichment started for {ticker.upper()}"}


def _run_batch_with_progress():
    """Wrapper around enrichment run_batch that tracks progress."""
    from enrichment_agent import get_conn as enrich_conn, get_prospects, detect_signals, generate_profile, save_results
    from asx_browser import ASXFetcher
    import time as _time

    _enrich_progress["running"] = True
    _enrich_progress["current"] = 0
    _enrich_progress["ok"] = 0
    _enrich_progress["skip"] = 0
    _enrich_progress["fail"] = 0
    _enrich_progress["ticker"] = ""

    conn = enrich_conn()
    try:
        prospects = get_prospects(conn)
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
                    save_results(conn, p, signals, profile, anns)
                    _enrich_progress["ok"] += 1
                except Exception as e:
                    logger.error(f"{tk}: {e}")
                    _enrich_progress["fail"] += 1
    except Exception as e:
        logger.error(f"Batch enrichment failed: {e}")
    finally:
        conn.close()
        _enrich_progress["running"] = False
        logger.info(f"Batch enrichment finished: {_enrich_progress['ok']} ok, {_enrich_progress['skip']} skip, {_enrich_progress['fail']} fail")


@app.post("/api/enrich/batch")
def trigger_batch_enrichment(background_tasks: BackgroundTasks):
    """Trigger batch enrichment for all unscreened/qualified prospects (runs in background)."""
    if _enrich_progress["running"]:
        return {"message": "Enrichment already running", "count": _enrich_progress["total"], "running": True}

    # Count how many will be processed
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM prospect_matrix WHERE status IN ('unscreened', 'qualified')"
            )
            count = cur.fetchone()[0]
    finally:
        put_conn(conn)

    if count == 0:
        return {"message": "No unscreened or qualified prospects to enrich", "count": 0}

    background_tasks.add_task(_run_batch_with_progress)
    return {"message": f"Batch enrichment started for {count} prospects", "count": count}


@app.get("/api/enrich/status")
def get_enrichment_status():
    """Return current enrichment progress."""
    return {
        "running": _enrich_progress["running"],
        "current": _enrich_progress["current"],
        "total": _enrich_progress["total"],
        "ticker": _enrich_progress["ticker"],
        "ok": _enrich_progress["ok"],
        "skip": _enrich_progress["skip"],
        "fail": _enrich_progress["fail"],
    }


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
