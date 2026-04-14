"""
Enrichment Agent — New Delta 6-Pillar Signal Detection
========================================================
Scans ASX announcement titles for keyword patterns mapped to
New Delta's 6 service pillars:
  1. Production (40%)
  2. License to Operate (20%)
  3. Cost (15%)
  4. People (15%)
  5. Quality (5%)
  6. Future Readiness (5%)

Usage:
    python enrichment_agent.py --mode batch
    python enrichment_agent.py --mode single --ticker BHP
    python enrichment_agent.py --mode rescore
"""

import logging, argparse, sys, os, time, re
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from asx_browser import ASXFetcher

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DELAY_ASX = 2.0   # Markit API rate limit — 2s between requests

def _parse_database_url(url: str) -> dict:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "dbname": (parsed.path or "/delta_prospect").lstrip("/"),
        "user": parsed.username or "delta",
        "password": parsed.password or "delta_dev",
    }

_database_url = os.getenv("DATABASE_URL")
DB_CONFIG = _parse_database_url(_database_url) if _database_url else {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME", "delta_prospect"),
    "user":     os.getenv("DB_USER", "delta"),
    "password": os.getenv("DB_PASSWORD", "delta_dev"),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("enrichment_agent")

# ---------------------------------------------------------------------------
# New Delta 6-Pillar Keyword Library
# ---------------------------------------------------------------------------
# Each entry: (regex_pattern, strength, summary_template)
# Patterns are matched case-insensitively against announcement titles.

PILLAR_PATTERNS = {
    "production": [
        # Strong signals — active failures / downgrades
        (r"production below guidance", "strong",
         "{company} reported production below guidance"),
        (r"missed targets?", "strong",
         "{company} missed production targets"),
        (r"downgraded outlook", "strong",
         "{company} downgraded its outlook"),
        (r"operational disruption", "strong",
         "{company} reported operational disruption"),
        (r"throughput below plan", "strong",
         "{company} reported throughput below plan"),
        (r"production guidance reduced", "strong",
         "{company} reduced production guidance"),
        (r"nameplate capacity not achieved", "strong",
         "{company} has not achieved nameplate capacity"),
        # Moderate signals — challenges / delays
        (r"weather impacts?", "moderate",
         "{company} reported weather impacts on operations"),
        (r"maintenance downtime", "moderate",
         "{company} reported maintenance downtime"),
        (r"ramp[\s-]?up slower than expected", "moderate",
         "{company} ramp-up is slower than expected"),
        (r"commissioning delay(?:s|ed)", "moderate",
         "{company} reported commissioning delays"),
        (r"logistics? constraints?", "moderate",
         "{company} facing logistics constraints"),
        (r"project delayed", "moderate",
         "{company} reported project delays"),
    ],
    "license_to_operate": [
        # Strong signals — serious incidents / regulatory action
        (r"TRIFR increase", "strong",
         "{company} reported TRIFR increase"),
        (r"AIFR increase", "strong",
         "{company} reported AIFR increase"),
        (r"repeatable incidents?", "strong",
         "{company} flagged repeatable safety incidents"),
        (r"safety performance below target", "strong",
         "{company} safety performance below target"),
        (r"unplanned shutdown due to safety", "strong",
         "{company} had unplanned shutdown due to safety"),
        (r"prohibition notice", "strong",
         "{company} received a prohibition notice"),
        (r"regulator intervention", "strong",
         "{company} subject to regulator intervention"),
        (r"environmental infringement", "strong",
         "{company} received environmental infringement notice"),
        (r"licen[cs]e breach", "strong",
         "{company} reported a licence breach"),
        # Moderate signals — emerging risks
        (r"environmental approval delay", "moderate",
         "{company} facing environmental approval delays"),
        (r"regulatory challenges?", "moderate",
         "{company} facing regulatory challenges"),
        (r"community (?:concerns?|opposition)", "moderate",
         "{company} facing community concerns or opposition"),
        (r"asset integrity issues?", "moderate",
         "{company} reported asset integrity issues"),
        (r"compliance issues?", "moderate",
         "{company} flagged compliance issues"),
        (r"assurance gaps?", "moderate",
         "{company} identified assurance gaps"),
        (r"improvement notice", "moderate",
         "{company} received an improvement notice"),
        (r"safety alert", "moderate",
         "{company} issued a safety alert"),
    ],
    "cost": [
        # Strong signals — material financial pressure
        (r"cost overruns?", "strong",
         "{company} reported cost overruns"),
        (r"capital blowouts?", "strong",
         "{company} reported capital blowouts"),
        (r"impairment", "strong",
         "{company} announced an asset impairment"),
        (r"write[\s-]?down", "strong",
         "{company} announced a write-down"),
        (r"capital rais(?:e|ing)", "strong",
         "{company} is raising capital — potential cash pressure"),
        (r"going concern", "strong",
         "{company} flagged going concern risk"),
        # Moderate signals — cost pressure indicators
        (r"cost pressures?", "moderate",
         "{company} flagged cost pressures"),
        (r"unit costs? increased", "moderate",
         "{company} reported unit cost increases"),
        (r"inflation impacts?", "moderate",
         "{company} impacted by inflation"),
        (r"margin compression", "moderate",
         "{company} experiencing margin compression"),
        (r"lower reali[sz]ed prices?", "moderate",
         "{company} reporting lower realised prices"),
        (r"inefficienc(?:y|ies)", "moderate",
         "{company} flagged operational inefficiencies"),
        (r"productivity decline", "moderate",
         "{company} reported productivity decline"),
        (r"budget exceeded", "moderate",
         "{company} exceeded budget"),
    ],
    "people": [
        # Strong signals — workforce crisis
        (r"labo[u]?r shortages?", "strong",
         "{company} facing labour shortages"),
        (r"industrial action", "strong",
         "{company} experiencing industrial action"),
        (r"managing director resigned", "strong",
         "{company} managing director resigned"),
        (r"skills? shortages?", "strong",
         "{company} flagged skills shortage"),
        # Moderate signals — workforce challenges
        (r"operator availability", "moderate",
         "{company} facing operator availability issues"),
        (r"capability gaps?", "moderate",
         "{company} identified capability gaps"),
        (r"contractor performance issues?", "moderate",
         "{company} reported contractor performance issues"),
        (r"low engagement", "moderate",
         "{company} reported low workforce engagement"),
        (r"high turnover", "moderate",
         "{company} experiencing high staff turnover"),
        (r"training required", "moderate",
         "{company} identified training requirements"),
        (r"leadership capability uplift", "moderate",
         "{company} undertaking leadership capability uplift"),
        (r"CEO appointed", "moderate",
         "{company} appointed new CEO"),
        (r"restructur(?:e|ing)", "moderate",
         "{company} announced a restructure"),
        (r"voluntary redundanc(?:y|ies)", "moderate",
         "{company} offering voluntary redundancies"),
        (r"enterprise agreement", "moderate",
         "{company} negotiating enterprise agreement"),
    ],
    "quality": [
        # Strong signals — serious quality failures
        (r"product recall", "strong",
         "{company} issued a product recall"),
        (r"customer dispute", "strong",
         "{company} facing a customer dispute"),
        (r"warranty claim", "strong",
         "{company} facing warranty claims"),
        # Moderate signals — quality issues
        (r"non[\s-]?conformance(?:s)?\s*(?:increasing)?", "moderate",
         "{company} reported non-conformances"),
        (r"rework", "moderate",
         "{company} reported rework requirements"),
        (r"defects?", "moderate",
         "{company} reported defects"),
        (r"processing issues?", "moderate",
         "{company} reported processing issues"),
        (r"plant reliability issues?", "moderate",
         "{company} reported plant reliability issues"),
        (r"data inconsistenc(?:y|ies)", "moderate",
         "{company} flagged data inconsistency"),
        (r"reporting discrepanc(?:y|ies)", "moderate",
         "{company} flagged reporting discrepancies"),
        (r"commissioning issues?", "moderate",
         "{company} reported commissioning issues"),
        (r"performance variability", "moderate",
         "{company} reported performance variability"),
        (r"quality audit", "weak",
         "{company} undergoing a quality audit"),
    ],
    "future_readiness": [
        # Strong signals — governance / strategic dysfunction
        (r"governance issues?", "strong",
         "{company} flagged governance issues"),
        (r"lack of alignment", "strong",
         "{company} flagged lack of alignment"),
        (r"fragmented decision making", "strong",
         "{company} suffering from fragmented decision making"),
        # Moderate signals — organisational friction
        (r"silos?", "moderate",
         "{company} facing organisational silos"),
        (r"poor communication", "moderate",
         "{company} flagged poor communication"),
        (r"unclear strategy", "moderate",
         "{company} has unclear strategy"),
        (r"lack of visibility", "moderate",
         "{company} flagged lack of visibility"),
        (r"reactive management", "moderate",
         "{company} operating in reactive management mode"),
        (r"unclear roles and responsibilities", "moderate",
         "{company} has unclear roles and responsibilities"),
        (r"slow decision making", "moderate",
         "{company} suffering from slow decision making"),
    ],
}

# Pillar weights (for scoring context — actual DB scoring is in SQL function)
PILLAR_WEIGHTS = {
    "production": 0.40,
    "license_to_operate": 0.20,
    "cost": 0.15,
    "people": 0.15,
    "quality": 0.05,
    "future_readiness": 0.05,
}

# ---------------------------------------------------------------------------
# Strategic Profile Templates
# ---------------------------------------------------------------------------

SECTOR_PROFILES = {
    "Energy": {
        "direction": "Navigate energy transition while maintaining hydrocarbon production",
        "tailwind": "Strong energy demand and commodity price environment",
        "headwind": "Regulatory and ESG pressure on fossil fuel operations",
    },
    "Materials": {
        "direction": "Optimise mining operations and resource extraction efficiency",
        "tailwind": "Global demand for metals and critical minerals",
        "headwind": "Operational complexity, cost inflation, and permitting challenges",
    },
    "Industrials": {
        "direction": "Scale industrial services and infrastructure delivery",
        "tailwind": "Government infrastructure spending and defence investment",
        "headwind": "Supply chain constraints and skilled labour shortages",
    },
    "Utilities": {
        "direction": "Transition generation portfolio toward renewables",
        "tailwind": "Policy support for clean energy transition",
        "headwind": "Grid reliability pressure during energy transition",
    },
}
DEFAULT_PROFILE = {
    "direction": "Stabilise operations and grow market position",
    "tailwind": "Sector tailwinds from commodity demand",
    "headwind": "Operational and cost pressures",
}

# Headwind overrides based on dominant pillar
PILLAR_HEADWIND_OVERRIDES = {
    "production": "Production delivery and operational reliability challenges",
    "license_to_operate": "Safety, environmental, and regulatory compliance risks",
    "cost": "Cost inflation and margin pressure",
    "people": "Labour shortages and workforce capability challenges",
    "quality": "Quality control and plant reliability issues",
    "future_readiness": "Governance and strategic alignment challenges",
}

# ---------------------------------------------------------------------------
# Pattern Engine
# ---------------------------------------------------------------------------

def detect_signals(company_name: str, announcements: list[dict]) -> list[dict]:
    signals = []
    seen = set()

    for ann in announcements:
        title = ann.get("title", "")
        if not title:
            continue
        for pillar, patterns in PILLAR_PATTERNS.items():
            for pattern, strength, summary_tpl in patterns:
                if re.search(pattern, title, re.IGNORECASE):
                    key = (pillar, pattern)
                    if key in seen:
                        continue
                    seen.add(key)
                    signals.append({
                        "pressure_type": pillar,
                        "strength": strength,
                        "summary": summary_tpl.format(company=company_name),
                        "extracted_quote": title[:200],
                        "source_title": title,
                        "source_url": ann.get("url", ""),
                        "source_date": ann.get("date", ""),
                        "confidence": {"strong": 0.85, "moderate": 0.65, "weak": 0.40}.get(strength, 0.5),
                    })

    strength_order = {"strong": 0, "moderate": 1, "weak": 2}
    signals.sort(key=lambda s: strength_order.get(s["strength"], 9))

    logger.info(f"{company_name}: {len(signals)} signals "
                f"({sum(1 for s in signals if s['strength']=='strong')} strong, "
                f"{sum(1 for s in signals if s['strength']=='moderate')} mod, "
                f"{sum(1 for s in signals if s['strength']=='weak')} weak)")
    return signals


def calculate_lead_tier(signals: list[dict]) -> str:
    """Calculate lead tier based on signal distribution across pillars."""
    if not signals:
        return "not_qualified"

    # Count pillars with strong signals
    pillars_strong = set()
    pillars_moderate = set()
    for s in signals:
        if s["strength"] == "strong":
            pillars_strong.add(s["pressure_type"])
        if s["strength"] in ("strong", "moderate"):
            pillars_moderate.add(s["pressure_type"])

    if len(pillars_strong) >= 3:
        return "hot"
    elif len(pillars_strong) >= 1 or len(pillars_moderate) >= 3:
        return "warm"
    else:
        return "watch"


def generate_profile(sector: str, signals: list[dict]) -> dict:
    base = SECTOR_PROFILES.get(sector, DEFAULT_PROFILE)
    if not signals:
        likelihood = 3
    else:
        strong = sum(1 for s in signals if s["strength"] == "strong")
        moderate = sum(1 for s in signals if s["strength"] == "moderate")
        likelihood = min(10, max(1, int(strong * 3 + moderate * 1.5 + len(signals) * 0.5)))

    # Determine dominant pillar
    type_counts = {}
    for s in signals:
        type_counts[s["pressure_type"]] = type_counts.get(s["pressure_type"], 0) + 1

    headwind = base["headwind"]
    if type_counts:
        dominant = max(type_counts, key=type_counts.get)
        headwind = PILLAR_HEADWIND_OVERRIDES.get(dominant, headwind)

    return {
        "strategic_direction": base["direction"],
        "primary_tailwind": base["tailwind"],
        "primary_headwind": headwind,
        "likelihood_score": likelihood,
    }

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_conn():
    return psycopg2.connect(**DB_CONFIG)

VALID_PT = {"production", "license_to_operate", "cost", "people", "quality", "future_readiness"}
VALID_ST = {"weak", "moderate", "strong"}

def get_prospects(conn, statuses=None, ticker=None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if ticker:
            cur.execute("""
                SELECT pm.id prospect_id, pm.status, l.id listing_id,
                       l.ticker, l.company_name, l.gics_sector
                FROM prospect_matrix pm JOIN asx_listings l ON l.id=pm.listing_id
                WHERE l.ticker=%s AND l.is_active=TRUE
            """, (ticker.upper(),))
        else:
            cur.execute("""
                SELECT pm.id prospect_id, pm.status, l.id listing_id,
                       l.ticker, l.company_name, l.gics_sector
                FROM prospect_matrix pm JOIN asx_listings l ON l.id=pm.listing_id
                WHERE pm.status=ANY(%s::prospect_status[]) AND l.is_active=TRUE
                ORDER BY l.market_cap_aud DESC NULLS LAST
            """, (statuses or ["unscreened", "qualified"],))
        return cur.fetchall()


def save_results(conn, prospect, signals, profile, announcements):
    pid, lid, tk = prospect["prospect_id"], prospect["listing_id"], prospect["ticker"]
    with conn.cursor() as cur:
        inserted = 0
        for s in signals:
            pt, st = s["pressure_type"].lower(), s["strength"].lower()
            if pt not in VALID_PT or st not in VALID_ST:
                continue
            try:
                cur.execute("""
                    INSERT INTO pressure_signals
                        (prospect_id, pressure_type, strength, summary,
                         source_type, source_url, source_title, source_date,
                         confidence_score, model_version, extracted_quote)
                    VALUES (%s,%s,%s,%s,'asx_announcement',%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (prospect_id, pressure_type, source_url) DO NOTHING
                """, (pid, pt, st, s["summary"], s.get("source_url"),
                      s.get("source_title"), s.get("source_date") or None,
                      s["confidence"], "rule-engine-v2", s.get("extracted_quote")))
                inserted += cur.rowcount
            except Exception as e:
                logger.error(f"{tk}: {e}")

        lk = max(1, min(10, int(profile["likelihood_score"])))
        lead_tier = calculate_lead_tier(signals)

        cur.execute("""
            UPDATE prospect_matrix SET
                strategic_direction=COALESCE(%s,strategic_direction),
                primary_tailwind=COALESCE(%s,primary_tailwind),
                primary_headwind=COALESCE(%s,primary_headwind),
                likelihood_score=COALESCE(%s,likelihood_score),
                lead_tier=%s,
                status=CASE WHEN status IN ('unscreened','qualified')
                    THEN 'enriched'::prospect_status ELSE status END,
                status_changed_by='enrichment_agent'
            WHERE id=%s
        """, (profile["strategic_direction"], profile["primary_tailwind"],
              profile["primary_headwind"], lk, lead_tier, pid))

        cur.execute("SELECT calculate_prospect_score(%s)", (pid,))
        score = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO enrichment_log
                (listing_id, action, source_type, success,
                 documents_processed, signals_found,
                 triggered_by, completed_at, agent_version)
            VALUES (%s,'announcement_scan','asx_announcement',TRUE,%s,%s,
                    'enrichment_agent',NOW(),'rule-engine-v2')
        """, (lid, len(announcements), inserted))
        conn.commit()
        logger.info(f"{tk}: {inserted} signals, likelihood={lk}, tier={lead_tier}, score={score}")


def rescore_all(conn):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id FROM prospect_matrix WHERE status IN ('enriched','ready_for_outreach')")
        rows = cur.fetchall()
        for r in rows:
            cur.execute("SELECT calculate_prospect_score(%s)", (r["id"],))
            cur.execute("SELECT calculate_lead_tier(%s)", (r["id"],))
        conn.commit()
        logger.info(f"Rescored {len(rows)} prospects")

# ---------------------------------------------------------------------------
# Entry Points
# ---------------------------------------------------------------------------

def run_batch():
    logger.info("=" * 60)
    logger.info("BATCH ENRICHMENT (6-pillar, rule-based, $0 cost) — START")
    logger.info("=" * 60)
    conn = get_conn()
    try:
        prospects = get_prospects(conn)
        logger.info(f"{len(prospects)} prospects to enrich")
        ok, skip, fail = 0, 0, 0
        with ASXFetcher() as fetcher:
            for i, p in enumerate(prospects):
                tk = p["ticker"]
                logger.info(f"[{i+1}/{len(prospects)}] {tk} ({p['company_name']})")
                try:
                    anns = fetcher.fetch_announcements(tk)
                    time.sleep(DELAY_ASX)
                    if not anns:
                        skip += 1
                        continue
                    signals = detect_signals(p["company_name"], anns)
                    profile = generate_profile(p["gics_sector"], signals)
                    save_results(conn, p, signals, profile, anns)
                    ok += 1
                except Exception as e:
                    logger.error(f"{tk}: {e}")
                    fail += 1
        logger.info("=" * 60)
        logger.info(f"DONE — {ok} enriched, {skip} skipped, {fail} failed")
        logger.info("=" * 60)
    finally:
        conn.close()


def run_single(ticker):
    conn = get_conn()
    try:
        rows = get_prospects(conn, ticker=ticker)
        if not rows:
            logger.error(f"No prospect for {ticker}")
            return
        p = rows[0]
        with ASXFetcher() as fetcher:
            anns = fetcher.fetch_announcements(ticker.upper())
        if not anns:
            logger.info(f"{ticker}: no announcements")
            return
        signals = detect_signals(p["company_name"], anns)
        profile = generate_profile(p["gics_sector"], signals)
        save_results(conn, p, signals, profile, anns)
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    pa = argparse.ArgumentParser(description="Enrichment Agent (6-Pillar, Rule-Based, Free)")
    pa.add_argument("--mode", choices=["batch", "single", "rescore"], required=True)
    pa.add_argument("--ticker", type=str)
    args = pa.parse_args()
    if args.mode == "single" and not args.ticker:
        pa.error("--ticker required")
    try:
        if args.mode == "batch": run_batch()
        elif args.mode == "single": run_single(args.ticker)
        else:
            conn = get_conn()
            try: rescore_all(conn)
            finally: conn.close()
    except Exception as e:
        logger.exception(f"Failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
