"""
Enrichment Agent — Rule-Based Pressure Signal Detection
=========================================================
Scans ASX announcement titles for keyword patterns to detect
operational, cost, safety, governance, environmental, market,
and workforce pressure signals. No API costs. $0 to run.

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

DB_CONFIG = {
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
# Keyword Pattern Library
# ---------------------------------------------------------------------------

PRESSURE_PATTERNS = {
    "operational": [
        (r"production\s+(downgrade|cut|halt|suspen|curtail|shut)", "strong",
         "{company} announced a production downgrade or suspension"),
        (r"force\s+majeure", "strong",
         "{company} declared force majeure on operations"),
        (r"unplanned\s+(outage|shutdown|downtime|maintenance)", "strong",
         "{company} reported unplanned operational downtime"),
        (r"processing\s+(issue|problem|failure|interrupt)", "strong",
         "{company} reported processing issues"),
        (r"mine\s+(closure|shut|suspend)", "strong",
         "{company} announced mine closure or suspension"),
        (r"plant\s+(closure|shut|suspend)", "strong",
         "{company} announced plant closure or suspension"),
        (r"production\s+(report|update|result)", "moderate",
         "{company} released a production update"),
        (r"quarterly\s+activit", "moderate",
         "{company} released quarterly activities report"),
        (r"operational\s+(update|review|report)", "moderate",
         "{company} released an operational update"),
        (r"ramp[\s-]?up", "moderate",
         "{company} is in an operational ramp-up phase"),
        (r"commissioning", "moderate",
         "{company} is commissioning new operations"),
        (r"throughput", "moderate",
         "{company} reported on processing throughput"),
        (r"ore\s+reserve", "moderate",
         "{company} updated ore reserve estimates"),
        (r"exploration\s+(update|result)", "weak",
         "{company} released exploration results"),
        (r"drilling\s+(result|update|program)", "weak",
         "{company} released drilling results"),
    ],
    "cost": [
        (r"cost\s+(overrun|blowout|escala|increase|pressure)", "strong",
         "{company} flagged cost pressures or overruns"),
        (r"impairment", "strong",
         "{company} announced an asset impairment"),
        (r"write[\s-]?(down|off)", "strong",
         "{company} announced a write-down"),
        (r"loss\s+(after|before|for|of)", "strong",
         "{company} reported a financial loss"),
        (r"capital\s+raising", "strong",
         "{company} is raising capital, potential cash pressure"),
        (r"debt\s+(restructur|refinanc|facility|breach)", "strong",
         "{company} is restructuring debt obligations"),
        (r"cost\s+(reduc|cut|saving|optimi)", "moderate",
         "{company} announced cost reduction initiatives"),
        (r"capex\s+(reduc|cut|review|defer)", "moderate",
         "{company} is reducing or deferring capital expenditure"),
        (r"guidance\s+(downgrade|reduc|cut|lower|revis)", "moderate",
         "{company} downgraded financial guidance"),
        (r"profit\s+(warning|downgrade|decline)", "moderate",
         "{company} issued a profit warning"),
        (r"budget\s+(review|overrun|pressure)", "moderate",
         "{company} flagged budget concerns"),
        (r"restructur", "moderate",
         "{company} announced a restructuring program"),
        (r"half\s+year\s+result", "weak",
         "{company} released half year financial results"),
        (r"annual\s+report", "weak",
         "{company} released its annual report"),
        (r"quarterly\s+report", "weak",
         "{company} released quarterly financial report"),
    ],
    "safety": [
        (r"fatal", "strong",
         "{company} reported a fatality at operations"),
        (r"serious\s+(incident|injury|accident)", "strong",
         "{company} reported a serious safety incident"),
        (r"safety\s+(breach|violation|failure|incident|alert)", "strong",
         "{company} reported a safety incident or breach"),
        (r"regulator.{0,20}(action|penalty|fine|sanction|order|notice)", "strong",
         "{company} received regulatory action"),
        (r"prohibition\s+notice", "strong",
         "{company} received a prohibition notice"),
        (r"stop\s+work", "strong",
         "{company} issued or received a stop work order"),
        (r"safety\s+(review|audit|improvement|update)", "moderate",
         "{company} is conducting safety reviews"),
        (r"environmental\s+(incident|breach|spill|release)", "moderate",
         "{company} reported an environmental incident"),
        (r"compliance\s+(issue|review|update|breach)", "moderate",
         "{company} flagged compliance concerns"),
        (r"sustainability\s+report", "weak",
         "{company} released a sustainability report"),
        (r"safety\s+record", "weak",
         "{company} reported on safety performance"),
    ],
    "governance": [
        (r"(ceo|managing\s+director|md)\s+(resign|depart|terminat|remov|replac|step)", "strong",
         "{company} CEO/MD departure announced"),
        (r"board\s+(spill|dispute|challeng)", "strong",
         "{company} facing board-level governance challenges"),
        (r"class\s+action", "strong",
         "{company} facing class action proceedings"),
        (r"(asic|asx)\s+(query|investigation|inquiry|enforcement)", "strong",
         "{company} subject to regulatory inquiry"),
        (r"(cfo|director|chairman|chair)\s+(resign|depart|appoint|replac)", "moderate",
         "{company} announced leadership changes"),
        (r"board\s+(change|renewal|appointment|resign)", "moderate",
         "{company} announced board changes"),
        (r"strategy\s+(review|update|change|pivot|reset)", "moderate",
         "{company} announced a strategic review"),
        (r"(agm|annual\s+general\s+meeting)", "weak",
         "{company} held its annual general meeting"),
        (r"investor\s+presentation", "weak",
         "{company} released an investor presentation"),
        (r"corporate\s+governance", "weak",
         "{company} released corporate governance statement"),
    ],
    "environmental": [
        (r"tailings\s+(dam|storage|facility|breach|failure)", "strong",
         "{company} reported tailings-related issues"),
        (r"(spill|contamina|pollut)", "strong",
         "{company} reported an environmental contamination event"),
        (r"remediation", "strong",
         "{company} undertaking environmental remediation"),
        (r"epa.{0,15}(notice|order|action|penalty|fine)", "strong",
         "{company} received EPA enforcement action"),
        (r"(emission|carbon|climate)\s+(reduc|target|report|plan|risk)", "moderate",
         "{company} addressing emissions or climate risks"),
        (r"rehabilitation\s+(plan|program|provision|liabilit)", "moderate",
         "{company} addressing site rehabilitation obligations"),
        (r"water\s+(management|licen|usage|risk|scarcit)", "moderate",
         "{company} flagged water management concerns"),
        (r"environmental\s+(approval|permit|assessment|impact)", "moderate",
         "{company} navigating environmental approvals"),
        (r"esg\s+(report|update|rating|strateg)", "weak",
         "{company} released ESG information"),
        (r"sustainability", "weak",
         "{company} reported on sustainability"),
    ],
    "market": [
        (r"commodity\s+price\s+(crash|collapse|decline|drop|fall)", "strong",
         "{company} impacted by commodity price decline"),
        (r"(contract|offtake)\s+(loss|terminat|cancel|expir)", "strong",
         "{company} lost a key contract or offtake agreement"),
        (r"demand\s+(decline|drop|fall|weak|slow)", "strong",
         "{company} facing weakening market demand"),
        (r"(gold|iron\s+ore|copper|lithium|coal|oil|gas|nickel)\s+price", "moderate",
         "{company} exposed to commodity price movements"),
        (r"market\s+(update|outlook|condition|review)", "moderate",
         "{company} commented on market conditions"),
        (r"(contract|offtake)\s+(award|sign|secur|new|renew)", "moderate",
         "{company} secured new contracts"),
        (r"(export|trade|tariff|sanction)", "moderate",
         "{company} exposed to trade or export risks"),
        (r"commodity\s+(review|update|report)", "weak",
         "{company} reviewed commodity market conditions"),
    ],
    "workforce": [
        (r"(strike|industrial\s+action|work\s+stoppage)", "strong",
         "{company} experiencing industrial action"),
        (r"(redundanc|retrenchment|layoff|job\s+(cut|loss))", "strong",
         "{company} announced workforce reductions"),
        (r"(skill|labour|labor)\s+(shortage|crisis|gap|challenge)", "strong",
         "{company} flagged workforce shortage challenges"),
        (r"enterprise\s+(agreement|bargaining)", "moderate",
         "{company} negotiating enterprise agreements"),
        (r"workforce\s+(plan|strateg|review|restructur)", "moderate",
         "{company} restructuring workforce"),
        (r"(retention|turnover|attrition)", "moderate",
         "{company} addressing staff retention challenges"),
        (r"(hiring|recruitment|new\s+appoint)", "weak",
         "{company} making new appointments"),
    ],
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
        for pressure_type, patterns in PRESSURE_PATTERNS.items():
            for pattern, strength, summary_tpl in patterns:
                if re.search(pattern, title, re.IGNORECASE):
                    key = (pressure_type, pattern)
                    if key in seen:
                        continue
                    seen.add(key)
                    signals.append({
                        "pressure_type": pressure_type,
                        "strength": strength,
                        "summary": summary_tpl.format(company=company_name),
                        "extracted_quote": title[:200],
                        "source_title": title,
                        "source_url": ann.get("url", ""),
                        "source_date": ann.get("date", ""),
                        "confidence": {"strong": 0.80, "moderate": 0.60, "weak": 0.40}.get(strength, 0.5),
                    })

    strength_order = {"strong": 0, "moderate": 1, "weak": 2}
    signals.sort(key=lambda s: strength_order.get(s["strength"], 9))

    logger.info(f"{company_name}: {len(signals)} signals "
                f"({sum(1 for s in signals if s['strength']=='strong')} strong, "
                f"{sum(1 for s in signals if s['strength']=='moderate')} mod, "
                f"{sum(1 for s in signals if s['strength']=='weak')} weak)")
    return signals


def generate_profile(sector: str, signals: list[dict]) -> dict:
    base = SECTOR_PROFILES.get(sector, DEFAULT_PROFILE)
    if not signals:
        likelihood = 3
    else:
        strong = sum(1 for s in signals if s["strength"] == "strong")
        moderate = sum(1 for s in signals if s["strength"] == "moderate")
        likelihood = min(10, max(1, int(strong * 3 + moderate * 1.5 + len(signals) * 0.5)))

    type_counts = {}
    for s in signals:
        type_counts[s["pressure_type"]] = type_counts.get(s["pressure_type"], 0) + 1

    headwind = base["headwind"]
    if type_counts:
        dominant = max(type_counts, key=type_counts.get)
        overrides = {
            "operational": "Operational reliability and production challenges",
            "cost": "Cost inflation and margin pressure",
            "safety": "Safety incidents and regulatory compliance risks",
            "governance": "Leadership instability and governance concerns",
            "environmental": "Environmental compliance and remediation obligations",
            "market": "Commodity price volatility and demand uncertainty",
            "workforce": "Labour shortages and industrial relations challenges",
        }
        headwind = overrides.get(dominant, headwind)

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

VALID_PT = {"operational","cost","safety","governance","environmental","market","workforce"}
VALID_ST = {"weak","moderate","strong"}

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
            """, (statuses or ["unscreened","qualified"],))
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
                      s["confidence"], "rule-engine-v1", s.get("extracted_quote")))
                inserted += cur.rowcount
            except Exception as e:
                logger.error(f"{tk}: {e}")

        lk = max(1, min(10, int(profile["likelihood_score"])))
        cur.execute("""
            UPDATE prospect_matrix SET
                strategic_direction=COALESCE(%s,strategic_direction),
                primary_tailwind=COALESCE(%s,primary_tailwind),
                primary_headwind=COALESCE(%s,primary_headwind),
                likelihood_score=COALESCE(%s,likelihood_score),
                status=CASE WHEN status IN ('unscreened','qualified')
                    THEN 'enriched'::prospect_status ELSE status END,
                status_changed_by='enrichment_agent'
            WHERE id=%s
        """, (profile["strategic_direction"], profile["primary_tailwind"],
              profile["primary_headwind"], lk, pid))

        cur.execute("SELECT calculate_prospect_score(%s)", (pid,))
        score = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO enrichment_log
                (listing_id, action, source_type, success,
                 documents_processed, signals_found,
                 triggered_by, completed_at, agent_version)
            VALUES (%s,'announcement_scan','asx_announcement',TRUE,%s,%s,
                    'enrichment_agent',NOW(),'rule-engine-v1')
        """, (lid, len(announcements), inserted))
        conn.commit()
        logger.info(f"{tk}: {inserted} signals, likelihood={lk}, score={score}")


def rescore_all(conn):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id FROM prospect_matrix WHERE status IN ('enriched','ready_for_outreach')")
        rows = cur.fetchall()
        for r in rows:
            cur.execute("SELECT calculate_prospect_score(%s)", (r["id"],))
        conn.commit()
        logger.info(f"Rescored {len(rows)} prospects")

# ---------------------------------------------------------------------------
# Entry Points
# ---------------------------------------------------------------------------

def run_batch():
    logger.info("=" * 60)
    logger.info("BATCH ENRICHMENT (rule-based, $0 cost) — START")
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
    pa = argparse.ArgumentParser(description="Enrichment Agent (Rule-Based, Free)")
    pa.add_argument("--mode", choices=["batch","single","rescore"], required=True)
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
