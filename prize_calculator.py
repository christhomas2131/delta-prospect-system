"""
prize_calculator.py — Size of Prize Rules Engine
=================================================
Estimates the commercial problem size for a prospect based on their
pressure signals, using directional heuristics provided by New Delta.

Dollar values are intentionally approximate. Their purpose is ranking
and comparison, not financial precision.

Usage:
    from prize_calculator import calculate_size_of_prize
    result = calculate_size_of_prize(conn, prospect_id)
    # result = {
    #   "total_prize": 12500000,
    #   "breakdown_by_pillar": {"production": 7000000, "cost": 5500000},
    #   "top_3_contributors": [...],
    #   "deal_fit": "sweet_spot",
    # }
"""

import re
import logging
from psycopg2.extras import RealDictCursor

logger = logging.getLogger("prize_calculator")

# ---------------------------------------------------------------------------
# Rules engine: keyword → dollar value
# ---------------------------------------------------------------------------
# Each rule: (regex_pattern, dollar_value)
# Patterns are matched case-insensitively against the combined text of
# source_title + summary + extracted_quote for each signal.
# The first matching rule wins. Rules are ordered most-specific first.

SIGNAL_VALUE_RULES = [
    # --- LICENSE TO OPERATE ---------------------------------------------------
    # Fatality: highest value
    (r"fatal(ity|ities|)", 1_000_000),
    # Regulatory notices / enforcement
    (r"prohibition.notice", 350_000),
    (r"improvement.notice", 350_000),
    (r"stop.work", 350_000),
    (r"regulator.{0,20}(action|intervention|order|penalty|fine)", 350_000),
    (r"licen[cs]e.breach", 350_000),
    (r"(ASIC|ASX).{0,15}(query|investigation|inquiry|enforcement)", 200_000),
    (r"class.action", 200_000),
    # Environmental incidents
    (r"environmental.{0,20}(infringement|breach|spill|incident)", 500_000),
    (r"remediation", 200_000),
    (r"rehabilitation", 200_000),
    # Safety incidents
    (r"serious.incident|serious.injury|LTI|TRIFR", 250_000),
    # Community / reputation
    (r"community.{0,10}(concern|opposition|conflict)", 200_000),
    # Board
    (r"board.{0,10}(spill|dispute|challeng)", 200_000),

    # --- PRODUCTION -----------------------------------------------------------
    # Explicit production misses (use high default; actual revenue % not available)
    (r"production.{0,20}(below|miss|cut|halt|suspen|curtail|downgrad)", 5_000_000),
    (r"guidance.{0,20}(downgrad|reduc|cut|lower|revis)", 5_000_000),
    (r"below.guidance|missed.guidance", 5_000_000),
    (r"force.majeure", 2_000_000),
    (r"mine.{0,10}(closure|shut|suspend)", 2_000_000),
    # Project / commissioning delays
    (r"(commissioning|project).{0,20}(delay|issue|problem)", 2_000_000),
    (r"delay.{0,20}commissioning", 2_000_000),
    (r"major.projects?.update", 2_000_000),
    # Operational disruptions
    (r"operational.disruption", 1_000_000),
    (r"unplanned.(outage|shutdown|downtime)", 1_000_000),
    # Throughput issues
    (r"throughput.{0,20}(below|below.plan|issue|problem)", 3_000_000),
    (r"throughput", 3_000_000),

    # --- COST -----------------------------------------------------------------
    (r"going.concern", 3_000_000),
    (r"impairment", 5_000_000),
    (r"write.{0,5}(down|off)", 5_000_000),
    (r"capital.rais(e|ing)", 3_000_000),
    (r"debt.{0,10}(restructur|refinanc|breach)", 3_000_000),
    (r"cost.(overrun|blowout)|budget.exceed", 2_000_000),
    (r"profit.warning", 2_000_000),
    (r"loss.{0,15}(after|before|for|of).tax", 2_000_000),
    (r"margin.compression|unit.cost.increas", 1_500_000),
    (r"(cost|margin|price).{0,10}(review|reduc|pressure|increase)", 1_500_000),

    # --- PEOPLE ---------------------------------------------------------------
    (r"(CEO|managing.director|MD).{0,15}(resign|depart|terminat|remov|replac|step)", 500_000),
    (r"(strike|industrial.action|work.stoppage)", 750_000),
    (r"(redundanc|retrenchment|layoff|job.{0,5}(cut|loss))", 1_000_000),
    (r"labo[u]?r.shortage", 750_000),
    (r"enterprise.agreement", 500_000),
    (r"restructur", 1_000_000),

    # --- QUALITY --------------------------------------------------------------
    (r"product.recall", 400_000),
    (r"(customer|contract).dispute", 300_000),
    (r"warranty.claim", 300_000),
    (r"non.conformance|non-conformance", 250_000),
    (r"processing.(issue|problem|failure)", 250_000),
    (r"plant.reliability", 250_000),
    (r"commissioning.(issue|problem|delay)", 250_000),

    # --- FUTURE READINESS -----------------------------------------------------
    (r"strategic.(review|reset|pivot)", 200_000),
    (r"governance", 200_000),
]

# Default dollar values by (pillar, strength) when no keyword rule matches
DEFAULT_VALUES = {
    ("production",         "strong"):  2_000_000,
    ("production",         "moderate"):  500_000,
    ("production",         "weak"):      100_000,
    ("license_to_operate", "strong"):    350_000,
    ("license_to_operate", "moderate"):  150_000,
    ("license_to_operate", "weak"):       50_000,
    ("cost",               "strong"):  2_000_000,
    ("cost",               "moderate"):  500_000,
    ("cost",               "weak"):      100_000,
    ("people",             "strong"):    500_000,
    ("people",             "moderate"):  200_000,
    ("people",             "weak"):       50_000,
    ("quality",            "strong"):    300_000,
    ("quality",            "moderate"):  100_000,
    ("quality",            "weak"):       25_000,
    ("future_readiness",   "strong"):    200_000,
    ("future_readiness",   "moderate"):  100_000,
    ("future_readiness",   "weak"):       50_000,
}

PILLAR_LABELS = {
    "production":         "Production",
    "license_to_operate": "License to Operate",
    "cost":               "Cost",
    "people":             "People",
    "quality":            "Quality",
    "future_readiness":   "Future Readiness",
}

# ---------------------------------------------------------------------------
# Routine filing discount
# ---------------------------------------------------------------------------
# These patterns indicate administrative/routine ASX filings rather than
# actual operational incidents. Their dollar value is multiplied by 0.25.

ROUTINE_FILING_PATTERNS = [
    r"conference call",
    r"\bAGM\b|annual general meeting",
    r"investor presentation",
    r"annual report",
    r"appendix 4[DE]",
    r"half.year(ly)? report",
    r"\bdividend\b",
    r"cleansing notice",
    r"quotation of securities|cessation of securities|trading halt",
    r"appendix 3[XY]",          # Director interest notices (routine)
    r"change of director",
    r"becoming.{0,10}substantial holder|ceasing.{0,10}substantial holder",
    r"change in substantial holding",
    r"notice to.{0,10}(meeting|noteholder)",
    r"corporate governance statement",
]

ROUTINE_DISCOUNT = 0.25  # multiply dollar estimate by this for routine filings

KEEP_FULL_VALUE_PATTERNS = [
    # Even if the title looks routine, keep full value if these are present
    r"fatal|fatality",
    r"incident|injury|accident",
    r"impairment|write.?down|going concern",
    r"capital rais|restructur|redundanc|strike",
    r"environmental breach|infringement|spill",
    r"production.{0,20}(below|miss|downgrad|halt)",
    r"force majeure",
    r"stop work|prohibition notice|improvement notice",
]


def _is_routine_filing(source_title: str) -> bool:
    """Return True if this announcement is a routine administrative filing."""
    if not source_title:
        return False
    title = source_title.strip()
    # If any "keep full value" pattern matches, it's not routine
    for p in KEEP_FULL_VALUE_PATTERNS:
        if re.search(p, title, re.IGNORECASE):
            return False
    # Check routine patterns
    for p in ROUTINE_FILING_PATTERNS:
        if re.search(p, title, re.IGNORECASE):
            return True
    return False


def _signal_dollar_value(signal: dict) -> int:
    """Return the estimated dollar value for one signal."""
    text = " ".join(filter(None, [
        str(signal.get("source_title") or ""),
        str(signal.get("summary") or ""),
        str(signal.get("extracted_quote") or ""),
    ]))

    for pattern, value in SIGNAL_VALUE_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return value

    # Fall back to default by pillar + strength
    pillar = signal.get("pressure_type", "")
    strength = signal.get("strength", "weak")
    return DEFAULT_VALUES.get((pillar, strength), 100_000)


def calculate_size_of_prize(conn, prospect_id: str) -> dict:
    """
    Calculate the estimated commercial problem size for a prospect.

    Reads all valid pressure signals from the DB, maps each to a dollar
    value using the rules engine, and returns an aggregate picture.

    Returns:
        dict with keys:
          total_prize (int): total estimated dollar impact
          breakdown_by_pillar (dict): {pillar: total_dollars}
          top_3_contributors (list): [{pillar, summary, value}, ...]
          deal_fit (str): "sweet_spot" | "small" | "enterprise"
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT pressure_type, strength, summary, source_title, extracted_quote
                FROM pressure_signals
                WHERE prospect_id = %s
                  AND (is_valid IS NULL OR is_valid = TRUE)
                ORDER BY
                    CASE strength WHEN 'strong' THEN 0 WHEN 'moderate' THEN 1 ELSE 2 END
            """, (prospect_id,))
            signals = [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("Failed to fetch signals for prize calc %s: %s", prospect_id, exc)
        return _empty_result()

    if not signals:
        return _empty_result()

    # Calculate dollar value per signal
    valued = []
    non_routine_total = 0
    routine_total = 0

    for s in signals:
        base_value = _signal_dollar_value(s)
        source_title = s.get("source_title") or ""
        routine = _is_routine_filing(source_title)
        effective = int(base_value * ROUTINE_DISCOUNT) if routine else base_value

        valued.append({
            "pillar":    s["pressure_type"],
            "strength":  s["strength"],
            "summary":   s.get("summary") or "",
            "value":     effective,
            "routine":   routine,
        })

        if routine:
            routine_total += effective
        else:
            non_routine_total += effective

    # Community reputation multiplier: if 2+ license_to_operate signals
    # mention community/reputation, multiply their combined value by 1.5×
    community_signals = [
        v for v in valued
        if v["pillar"] == "license_to_operate"
        and re.search(r"community|reputation", v["summary"], re.IGNORECASE)
    ]
    community_multiplier = 1.5 if len(community_signals) >= 2 else 1.0

    # Aggregate by pillar (applying the community multiplier where relevant)
    breakdown: dict[str, int] = {}
    for v in valued:
        pillar = v["pillar"]
        effective = v["value"]
        if (
            pillar == "license_to_operate"
            and re.search(r"community|reputation", v["summary"], re.IGNORECASE)
        ):
            extra = int(effective * (community_multiplier - 1.0))
            effective += extra
            non_routine_total += extra if not v["routine"] else 0
        breakdown[pillar] = breakdown.get(pillar, 0) + effective
        v["effective_value"] = effective

    total = sum(breakdown.values())

    # Confidence: what proportion of the total comes from non-routine signals?
    if total == 0:
        confidence = "low"
        confidence_label = "Low confidence — no signals"
    else:
        non_routine_pct = non_routine_total / total
        if non_routine_pct >= 0.50:
            confidence = "high"
            confidence_label = "High confidence"
        elif non_routine_pct >= 0.25:
            confidence = "moderate"
            confidence_label = "Moderate confidence"
        else:
            confidence = "low"
            confidence_label = "Low confidence — mostly routine filings"

    # Top 3 contributors by individual signal value
    top_3 = sorted(valued, key=lambda x: x.get("effective_value", x["value"]), reverse=True)[:3]
    top_3_out = [
        {
            "pillar":   s["pillar"],
            "summary":  s["summary"],
            "value":    s.get("effective_value", s["value"]),
            "routine":  s.get("routine", False),
        }
        for s in top_3
    ]

    # Deal fit band (from client spec)
    if total >= 50_000_000:
        deal_fit = "enterprise"
    elif total >= 5_000_000:
        deal_fit = "sweet_spot"
    else:
        deal_fit = "small"

    return {
        "total_prize":         total,
        "breakdown_by_pillar": breakdown,
        "top_3_contributors":  top_3_out,
        "deal_fit":            deal_fit,
        "confidence":          confidence,
        "confidence_label":    confidence_label,
        "non_routine_total":   non_routine_total,
        "routine_total":       routine_total,
    }


def _empty_result() -> dict:
    return {
        "total_prize":         0,
        "breakdown_by_pillar": {},
        "top_3_contributors":  [],
        "deal_fit":            "small",
    }
