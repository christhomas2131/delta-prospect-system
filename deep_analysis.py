"""
deep_analysis.py — Claude-Powered Deep Intelligence
=====================================================
Premium enrichment layer that uses the Claude API to produce rich,
actionable intelligence for New Delta's business development team.

Runs on top of the existing rule-based enrichment engine.
Requires an Anthropic API key (~$0.03-0.05 per company).

Usage (via API):
    POST /api/prospects/{id}/deep-analysis
"""

import json
import logging
import re
import time

from asx_browser import ASXFetcher

logger = logging.getLogger("deep_analysis")

SYSTEM_PROMPT = (
    "You are a management consulting analyst specialising in mining, energy, and heavy industry "
    "in Australia. You are working for New Delta, a boutique operational consulting firm based "
    "in Brisbane that helps ASX-listed industrial companies solve Production, License to Operate, "
    "Cost, People, Quality, and Future Readiness challenges. "
    "Be direct, specific, and commercially minded. Reference actual announcements, not generic "
    "industry trends. This analysis is for internal use by New Delta's business development team."
)


def _build_prompt(
    company_name: str,
    ticker: str,
    sector: str,
    existing_signals: list,
    announcements: list,
    size_of_prize: int = 0,
    deal_fit: str = "",
) -> str:
    ann_lines = "\n".join(
        f"  [{a.get('date', '?')[:10]}] {a.get('title', '')}"
        for a in announcements
    ) or "  (none available)"

    if existing_signals:
        sig_lines = "\n".join(
            f"  [{i}] {s.get('pressure_type','?')}/{s.get('strength','?')}: {s.get('summary','')} "
            f"(source: {s.get('source_title','?')})"
            for i, s in enumerate(existing_signals)
        )
    else:
        sig_lines = "  (none detected by keyword engine)"

    prize_str = f"${size_of_prize/1_000_000:.1f}M" if size_of_prize >= 1_000_000 else (
        f"${size_of_prize/1_000:.0f}K" if size_of_prize > 0 else "Not calculated"
    )

    return f"""You are analysing {company_name} ({ticker}), an ASX-listed {sector} company, as a potential client for New Delta.

RECENT ASX ANNOUNCEMENTS (last 24 months):
{ann_lines}

DETECTED PRESSURE SIGNALS (rule-based engine):
{sig_lines}

CURRENT SIZE OF PRIZE ESTIMATE: {prize_str}
DEAL FIT: {deal_fit or 'Not classified'}

---

Provide the following analysis. Respond with valid JSON only — no markdown fences, no text outside the JSON.

{{
  "validated_signals": [
    {{"index": 0, "confirmed": true, "reasoning": "short reason"}}
  ],
  "new_signals": [
    {{
      "pressure_type": "production|license_to_operate|cost|people|quality|future_readiness",
      "strength": "strong|moderate|weak",
      "summary": "concise one-sentence description of the pressure",
      "reasoning": "what in the announcements led to this",
      "source_title": "the announcement title that triggered this"
    }}
  ],
  "refined_profile": {{
    "strategic_direction": "1-2 sentence description of what this company is trying to achieve right now based on their announcements",
    "key_pressures": "2-3 sentences on the most significant operational challenges. Be specific — reference actual announcements.",
    "nd_fit_assessment": "2-3 sentences on which New Delta pillars represent the strongest opportunity and why this company would benefit from New Delta's methodology specifically",
    "primary_tailwind": "strongest market or operational tailwind for this company",
    "primary_headwind": "strongest operational headwind or challenge",
    "likelihood_score": 7,
    "likelihood_reasoning": "1-2 sentences explaining the urgency score"
  }},
  "prize_assessment": "Is the estimated {prize_str} problem impact reasonable based on what you see in the announcements? If you think it should be higher or lower, say so briefly.",
  "outreach_hypothesis": "2-3 sentences framed as: We believe [company] is facing [specific problem] that is costing approximately [dollar range]. Our [specific methodology] can reduce this by [percentage range].",
  "red_flags": "Any reasons NOT to pursue this company (in administration, being acquired, too small, signals are stale, etc.). Write null if none."
}}"""


def run_deep_analysis(
    prospect_id: str,
    ticker: str,
    company_name: str,
    sector: str,
    existing_signals: list,
    api_key: str,
    size_of_prize: int = 0,
    deal_fit: str = "",
) -> dict:
    """
    Run Claude deep analysis for one company.

    Returns a dict with:
        validated_signals:  list of {index, confirmed, reasoning}
        new_signals:        list of new signal dicts
        refined_profile:    dict with strategic_direction, key_pressures, nd_fit_assessment,
                            tailwind, headwind, likelihood + extended fields
        prize_assessment:   str — Claude's validation of the Size of Prize estimate
        outreach_hypothesis: str — the core BD pitch hypothesis
        red_flags:          str|None — reasons not to pursue
        tokens_used:        int
        announcements:      list of fetched announcements (for logging)
        error:              str (only if something went wrong)
    """
    import anthropic

    # Fetch fresh announcements
    with ASXFetcher() as fetcher:
        announcements = fetcher.fetch_announcements(ticker)

    if not announcements:
        return {"error": f"No announcements available for {ticker}"}

    prompt = _build_prompt(
        company_name, ticker, sector,
        existing_signals, announcements,
        size_of_prize, deal_fit,
    )

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        return {"error": "Invalid API key — please check Settings"}
    except anthropic.RateLimitError:
        return {"error": "Anthropic rate limit hit — try again in a moment"}
    except Exception as exc:
        return {"error": f"Claude API error: {exc}"}

    tokens_used = response.usage.input_tokens + response.usage.output_tokens
    raw = response.content[0].text.strip()

    # Strip markdown fences if Claude wrapped the JSON
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                result = json.loads(m.group())
            except json.JSONDecodeError:
                return {"error": f"Claude returned non-JSON response: {raw[:300]}"}
        else:
            return {"error": f"Claude returned non-JSON response: {raw[:300]}"}

    result["tokens_used"] = tokens_used
    result["announcements"] = announcements
    logger.info(
        "%s: deep analysis complete — %d validated, %d new signals, %d tokens",
        ticker,
        len(result.get("validated_signals", [])),
        len(result.get("new_signals", [])),
        tokens_used,
    )
    return result
