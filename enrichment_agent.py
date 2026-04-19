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

import logging, argparse, sys, os, time, re, json
from datetime import datetime, timezone, timedelta
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from asx_browser import ASXFetcher

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DELAY_ASX = 2.0          # Markit API rate limit — 2s between requests
LOOKBACK_DAYS = 730      # ~24 months — skip announcements older than this

# NOTE: The Markit Digital API (asx.api.markitdigital.com) is hard-capped at
# 5 announcements per request. All count/page/date parameters are silently
# ignored. The system accumulates signals over time via the DB unique
# constraint (prospect_id, pressure_type, source_url) — each enrichment run
# adds new signals for any announcements not previously seen.

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
        # Strong — explicit production problems
        (r"production.{0,20}(below|miss|cut|halt|suspen|curtail|downgrad)", "strong",
         "{company} flagged production issues"),
        (r"operational disruption", "strong",
         "{company} reported operational disruption"),
        (r"force majeure", "strong",
         "{company} declared force majeure"),
        (r"unplanned (outage|shutdown|downtime)", "strong",
         "{company} reported unplanned downtime"),
        (r"mine.{0,10}(closure|shut|suspend)", "strong",
         "{company} announced mine closure or suspension"),
        # Moderate — operational reporting and updates
        (r"quarterly.{0,10}(report|activit|production)", "moderate",
         "{company} released quarterly operational report"),
        (r"operations?\s+update", "moderate",
         "{company} released an operations update"),
        (r"production\s+(report|update|result)", "moderate",
         "{company} released a production update"),
        (r"major projects?\s+update", "moderate",
         "{company} released a major projects update"),
        (r"commissioning", "moderate",
         "{company} reported on commissioning activities"),
        (r"ramp[\s-]?up", "moderate",
         "{company} in operational ramp-up phase"),
        (r"throughput", "moderate",
         "{company} reported on processing throughput"),
        (r"(ore|mineral)\s+reserve", "moderate",
         "{company} updated resource/reserve estimates"),
        (r"feasibility\s+(study|report)", "moderate",
         "{company} released a feasibility study"),
        # Weak — general operational mentions
        (r"exploration\s+(update|result)", "weak",
         "{company} released exploration results"),
        (r"drilling\s+(result|update|program)", "weak",
         "{company} released drilling results"),
        (r"(plant|smelter|refinery|facility)", "weak",
         "{company} reported on processing facility"),
        (r"(appraisal|development)\s+(success|update|result)", "weak",
         "{company} released development/appraisal update"),
    ],
    "license_to_operate": [
        # Strong — regulatory action / incidents
        (r"prohibition\s+notice", "strong",
         "{company} received a prohibition notice"),
        (r"improvement\s+notice", "strong",
         "{company} received an improvement notice"),
        (r"regulator.{0,15}(action|intervention|order|penalty|fine)", "strong",
         "{company} subject to regulatory action"),
        (r"(fatal|serious\s+incident|serious\s+injury)", "strong",
         "{company} reported a serious safety incident"),
        (r"stop\s+work", "strong",
         "{company} issued or received a stop work order"),
        (r"environmental\s+(infringement|breach|spill|incident)", "strong",
         "{company} reported an environmental incident"),
        (r"licen[cs]e\s+breach", "strong",
         "{company} reported a licence breach"),
        # Moderate — compliance/safety/environmental
        (r"safety", "moderate",
         "{company} made a safety-related announcement"),
        (r"environmental\s+(approval|assessment|impact|report)", "moderate",
         "{company} navigating environmental approvals"),
        (r"compliance", "moderate",
         "{company} made a compliance-related announcement"),
        (r"community\s+(concern|opposition|engagement)", "moderate",
         "{company} addressing community concerns"),
        (r"remediation", "moderate",
         "{company} undertaking environmental remediation"),
        (r"rehabilitation", "moderate",
         "{company} addressing site rehabilitation"),
        # Weak — ESG/sustainability
        (r"sustainability\s+(report|update)", "weak",
         "{company} released sustainability information"),
        (r"ESG", "weak",
         "{company} released ESG information"),
    ],
    "cost": [
        # Strong — material financial stress
        (r"impairment", "strong",
         "{company} announced an asset impairment"),
        (r"write[\s-]?(down|off)", "strong",
         "{company} announced a write-down"),
        (r"capital\s+rais(e|ing)", "strong",
         "{company} is raising capital"),
        (r"going\s+concern", "strong",
         "{company} flagged going concern risk"),
        (r"(loss|deficit)\s+(after|before|for|of)", "strong",
         "{company} reported a financial loss"),
        (r"debt\s+(restructur|refinanc|facility|breach)", "strong",
         "{company} restructuring debt obligations"),
        (r"cost\s+(overrun|blowout|escala)", "strong",
         "{company} reported cost overruns"),
        # Moderate — financial reporting & capital management
        (r"half[\s-]?year\s+(result|financial|report)", "moderate",
         "{company} released half year financial results"),
        (r"(full[\s-]?year|annual)\s+(result|financial|report)", "moderate",
         "{company} released annual financial results"),
        (r"(FY\d{2}|HY\d{2}|1H|2H).{0,15}(result|report|financial)", "moderate",
         "{company} released financial results"),
        (r"syndicated\s+facility", "moderate",
         "{company} arranged a syndicated debt facility"),
        (r"guidance\s+(downgrad|reduc|cut|lower|revis)", "moderate",
         "{company} downgraded financial guidance"),
        (r"profit\s+(warning|downgrad|decline)", "moderate",
         "{company} issued a profit warning"),
        (r"appendix\s+4D", "moderate",
         "{company} lodged half year financial report"),
        (r"(cost|margin|price)\s+(review|reduc|pressure|increase)", "moderate",
         "{company} flagged cost or margin pressures"),
        # Weak — routine financial activity
        (r"buy[\s-]?back", "weak",
         "{company} conducting a share buy-back"),
        (r"dividend", "weak",
         "{company} made a dividend announcement"),
        (r"quotation\s+of\s+securities", "weak",
         "{company} applied for quotation of securities"),
        (r"cessation\s+of\s+securities", "weak",
         "{company} reported cessation of securities"),
    ],
    "people": [
        # Strong — leadership disruption
        (r"(CEO|managing\s+director|MD)\s+(resign|depart|terminat|remov|replac|step)", "strong",
         "{company} CEO/MD departure announced"),
        (r"(strike|industrial\s+action|work\s+stoppage)", "strong",
         "{company} experiencing industrial action"),
        (r"(redundanc|retrenchment|layoff|job.{0,5}(cut|loss))", "strong",
         "{company} announced workforce reductions"),
        (r"labo[u]?r\s+shortage", "strong",
         "{company} facing labour shortages"),
        # Moderate — leadership and workforce changes
        (r"(CEO|managing\s+director|MD|CFO|director|chairman|chair)\s+(appoint|resign|change)", "moderate",
         "{company} announced leadership changes"),
        (r"restructur", "moderate",
         "{company} announced a restructure"),
        (r"enterprise\s+agreement", "moderate",
         "{company} negotiating enterprise agreement"),
        (r"change\s+of\s+director", "moderate",
         "{company} announced a change of director"),
        (r"appendix\s+3X", "moderate",
         "{company} lodged initial director interest notice"),
        # Weak — routine director/people filings
        (r"appendix\s+3Y", "weak",
         "{company} lodged director interest change notice"),
        (r"change\s+in\s+substantial\s+holding", "weak",
         "{company} reported change in substantial holding"),
        (r"(becoming|ceasing)\s+(to\s+be\s+)?a?\s*substantial\s+holder", "weak",
         "{company} reported substantial holder change"),
    ],
    "quality": [
        # Strong — product/operational failures
        (r"product\s+recall", "strong",
         "{company} issued a product recall"),
        (r"(customer|contract)\s+dispute", "strong",
         "{company} facing a customer or contract dispute"),
        (r"warranty\s+claim", "strong",
         "{company} facing warranty claims"),
        # Moderate — quality/reliability indicators
        (r"(non[\s-]?conformance|defect)", "moderate",
         "{company} reported quality non-conformances"),
        (r"processing\s+(issue|problem|failure)", "moderate",
         "{company} reported processing issues"),
        (r"plant\s+reliability", "moderate",
         "{company} reported plant reliability issues"),
        (r"commissioning\s+(issue|problem|delay)", "moderate",
         "{company} reported commissioning issues"),
        (r"(rework|reject)", "moderate",
         "{company} reported rework requirements"),
        # Weak — quality-adjacent
        (r"(performance|operational)\s+(review|variability|improvement)", "weak",
         "{company} conducting performance review"),
        (r"(audit|assurance)", "weak",
         "{company} undertaking audit/assurance activities"),
    ],
    "future_readiness": [
        # Strong — governance/strategic problems
        (r"(ASIC|ASX)\s+(query|investigation|inquiry|enforcement)", "strong",
         "{company} subject to regulatory inquiry"),
        (r"class\s+action", "strong",
         "{company} facing class action proceedings"),
        (r"board\s+(spill|dispute|challeng)", "strong",
         "{company} facing board-level governance challenges"),
        # Moderate — strategic/governance activity
        (r"(final\s+)?investment\s+decision", "moderate",
         "{company} made an investment decision"),
        (r"strate(gy|gic)\s+(review|update|change|pivot|reset|partner)", "moderate",
         "{company} announced strategic activity"),
        (r"(joint\s+venture|partnership|JV|alliance)", "moderate",
         "{company} announced a strategic partnership"),
        (r"(acquisition|merger|takeover|bid)", "moderate",
         "{company} involved in M&A activity"),
        (r"(annual|extraordinary)\s+general\s+meeting|AGM|EGM", "moderate",
         "{company} holding a general meeting"),
        (r"investor\s+presentation", "moderate",
         "{company} released an investor presentation"),
        (r"(ownership|control)\s+(consol|change|transfer)", "moderate",
         "{company} undergoing ownership changes"),
        # Weak — routine governance
        (r"notice\s+(to|of)\s+(noteholder|meeting)", "weak",
         "{company} issued a notice to stakeholders"),
        (r"corporate\s+governance", "weak",
         "{company} released corporate governance statement"),
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
    """
    Scan announcement titles for pressure signal patterns.

    Deduplication key: (pillar, pattern, source_url) — this allows the same
    pattern to fire on DIFFERENT announcements (building up signal history
    over multiple enrichment runs), while still preventing the same pattern
    from duplicating on the same announcement URL within a single batch.

    The DB UNIQUE constraint (prospect_id, pressure_type, source_url) enforces
    true deduplication across runs: only the first signal per (pillar, URL)
    is stored regardless of which pattern triggered it.

    24-month date filter: announcements older than LOOKBACK_DAYS are skipped.
    """
    signals = []
    seen = set()

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    for ann in announcements:
        # --- 24-month date filter ---
        ann_date_str = ann.get("date", "")
        if ann_date_str:
            try:
                ann_dt = datetime.fromisoformat(ann_date_str.replace("Z", "+00:00"))
                if ann_dt.tzinfo is None:
                    ann_dt = ann_dt.replace(tzinfo=timezone.utc)
                if ann_dt < cutoff:
                    continue
            except (ValueError, TypeError):
                pass  # keep if date can't be parsed

        title = ann.get("title", "")
        if not title:
            continue
        ann_url = ann.get("url", "")

        for pillar, patterns in PILLAR_PATTERNS.items():
            for pattern, strength, summary_tpl in patterns:
                if re.search(pattern, title, re.IGNORECASE):
                    # Key includes the URL so each announcement independently
                    # triggers the pattern (different URLs → different signals)
                    key = (pillar, pattern, ann_url)
                    if key in seen:
                        continue
                    seen.add(key)
                    signals.append({
                        "pressure_type": pillar,
                        "strength": strength,
                        "summary": summary_tpl.format(company=company_name),
                        "extracted_quote": title[:200],
                        "source_title": title,
                        "source_url": ann_url,
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
                       l.ticker, l.company_name, l.gics_sector,
                       l.principal_activities
                FROM prospect_matrix pm JOIN asx_listings l ON l.id=pm.listing_id
                WHERE l.ticker=%s AND l.is_active=TRUE
            """, (ticker.upper(),))
        else:
            cur.execute("""
                SELECT pm.id prospect_id, pm.status, l.id listing_id,
                       l.ticker, l.company_name, l.gics_sector,
                       l.principal_activities
                FROM prospect_matrix pm JOIN asx_listings l ON l.id=pm.listing_id
                WHERE pm.status=ANY(%s::prospect_status[]) AND l.is_active=TRUE
                ORDER BY l.market_cap_aud DESC NULLS LAST
            """, (statuses or ["unscreened", "qualified"],))
        return cur.fetchall()


def _detect_location(principal_activities: str) -> dict:
    """
    Detect registered city and state from principal_activities text.
    Returns {'city': str|None, 'state': str|None, 'in_australia': True}
    All ASX-listed companies are treated as in_australia=True by default.
    """
    text = (principal_activities or "").lower()

    # City patterns — ordered specific-to-general, each maps to (city, state)
    CITY_PATTERNS = [
        (r"\bgold coast\b",   "Gold Coast",   "QLD"),
        (r"\btownsville\b",   "Townsville",   "QLD"),
        (r"\bcairns\b",       "Cairns",       "QLD"),
        (r"\bbrisbane\b",     "Brisbane",     "QLD"),
        (r"\bport hedland\b", "Port Hedland", "WA"),
        (r"\bkarratha\b",     "Karratha",     "WA"),
        (r"\bkalgoorlie\b",   "Kalgoorlie",   "WA"),
        (r"\bperth\b",        "Perth",        "WA"),
        (r"\bnewcastle\b",    "Newcastle",    "NSW"),
        (r"\bwollongong\b",   "Wollongong",   "NSW"),
        (r"\bsydney\b",       "Sydney",       "NSW"),
        (r"\bgeelong\b",      "Geelong",      "VIC"),
        (r"\bmelbourne\b",    "Melbourne",    "VIC"),
        (r"\badelaide\b",     "Adelaide",     "SA"),
        (r"\bdarwin\b",       "Darwin",       "NT"),
        (r"\bhobart\b",       "Hobart",       "TAS"),
        (r"\bcanberra\b",     "Canberra",     "ACT"),
    ]

    STATE_PATTERNS = [
        (r"\bqld\b|queensland",        "QLD"),
        (r"\bwa\b|western australia",  "WA"),
        (r"\bnsw\b|new south wales",   "NSW"),
        (r"\bvic\b|victoria",          "VIC"),
        (r"\bsa\b|south australia",    "SA"),
        (r"\bnt\b|northern territory", "NT"),
        (r"\btas\b|tasmania",          "TAS"),
        (r"\bact\b",                   "ACT"),
    ]

    city = None
    state = None

    for pattern, city_name, city_state in CITY_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            city = city_name
            state = city_state
            break

    if not state:
        for pattern, state_code in STATE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                state = state_code
                break

    return {"city": city, "state": state, "in_australia": True}


def save_results(conn, prospect, signals, profile, announcements, principal_activities=""):
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
        loc = _detect_location(principal_activities)

        cur.execute("""
            UPDATE prospect_matrix SET
                strategic_direction=COALESCE(%s,strategic_direction),
                primary_tailwind=COALESCE(%s,primary_tailwind),
                primary_headwind=COALESCE(%s,primary_headwind),
                likelihood_score=COALESCE(%s,likelihood_score),
                lead_tier=%s,
                registered_city=COALESCE(%s,registered_city),
                registered_state=COALESCE(%s,registered_state),
                in_australia=%s,
                status=CASE WHEN status IN ('unscreened','qualified')
                    THEN 'enriched'::prospect_status ELSE status END,
                status_changed_by='enrichment_agent'
            WHERE id=%s
        """, (profile["strategic_direction"], profile["primary_tailwind"],
              profile["primary_headwind"], lk, lead_tier,
              loc["city"], loc["state"], loc["in_australia"], pid))

        cur.execute("SELECT calculate_prospect_score(%s)", (pid,))
        score = cur.fetchone()[0]

        # Calculate Size of Prize — runs in same transaction so uncommitted
        # signals are visible to the query
        try:
            from prize_calculator import calculate_size_of_prize
            prize = calculate_size_of_prize(conn, pid)
            cur.execute("""
                UPDATE prospect_matrix SET
                    size_of_prize = %s,
                    prize_breakdown = %s
                WHERE id = %s
            """, (prize["total_prize"], json.dumps(prize), pid))
        except Exception as e:
            logger.warning(f"{tk}: prize calc failed (non-fatal): {e}")

        cur.execute("""
            INSERT INTO enrichment_log
                (listing_id, action, source_type, success,
                 documents_processed, signals_found,
                 triggered_by, completed_at, agent_version)
            VALUES (%s,'announcement_scan','asx_announcement',TRUE,%s,%s,
                    'enrichment_agent',NOW(),'rule-engine-v2')
        """, (lid, len(announcements), inserted))
        conn.commit()
        prize_fmt = f"${prize.get('total_prize', 0)/1e6:.1f}M" if 'prize' in dir() else "n/a"
        loc_fmt = f"{loc['city']}, {loc['state']}" if loc['city'] else (loc['state'] or "AU")
        logger.info(f"{tk}: {inserted} signals, likelihood={lk}, tier={lead_tier}, score={score}, prize={prize_fmt}, loc={loc_fmt}")


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
                    save_results(conn, p, signals, profile, anns, p.get("principal_activities") or "")
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
        save_results(conn, p, signals, profile, anns, p.get("principal_activities") or "")
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
