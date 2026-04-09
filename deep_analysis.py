"""
deep_analysis.py — Claude-Powered Pressure Signal Enhancement
=============================================================
Premium enrichment layer that uses the Claude API to validate rule-based
signals, detect missed signals, and generate refined strategic profiles.

Runs on top of the existing rule-based enrichment engine (enrichment_agent.py).
Requires an Anthropic API key (~$0.01-0.03 per company).

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
    "You are an expert industrial analyst specialising in ASX-listed mining, energy, "
    "and heavy industry companies. You identify subtle operational, financial, safety, "
    "governance, environmental, market, and workforce pressures from public disclosures. "
    "Be precise, evidence-based, and commercially minded."
)


def _build_prompt(company_name: str, ticker: str, sector: str,
                  existing_signals: list, announcements: list) -> str:
    ann_lines = "\n".join(
        f"  [{a.get('date', '?')}] {a.get('title', '')}"
        for a in announcements
    ) or "  (none available)"

    if existing_signals:
        sig_lines = "\n".join(
            f"  [{i}] {s.get('pressure_type','?')}/{s.get('strength','?')}: {s.get('summary','')}"
            for i, s in enumerate(existing_signals)
        )
    else:
        sig_lines = "  (none detected by keyword engine)"

    return f"""Analyse this ASX-listed company for operational and commercial pressures.

COMPANY: {company_name} ({ticker})
SECTOR: {sector}

RECENT ASX ANNOUNCEMENTS:
{ann_lines}

EXISTING RULE-BASED SIGNALS (keyword engine output):
{sig_lines}

Instructions:
1. VALIDATE each existing signal (by index). Confirm or deny based on actual announcement evidence.
2. DETECT NEW SIGNALS the keyword engine missed — subtle language, implications, cross-announcement patterns.
3. REFINED PROFILE: specific strategic direction, tailwind, and headwind for this company's actual situation.
4. LIKELIHOOD SCORE 1-10: how urgently does this company need external operational expertise?

Respond with valid JSON only — no markdown fences, no explanation outside the JSON structure:
{{
  "validated_signals": [
    {{"index": 0, "confirmed": true, "reasoning": "short reason"}}
  ],
  "new_signals": [
    {{
      "pressure_type": "operational|cost|safety|governance|environmental|market|workforce",
      "strength": "strong|moderate|weak",
      "summary": "concise one-sentence description of the pressure",
      "reasoning": "what in the announcements led to this signal",
      "source_title": "the announcement title that triggered this"
    }}
  ],
  "refined_profile": {{
    "strategic_direction": "...",
    "primary_tailwind": "...",
    "primary_headwind": "...",
    "likelihood_score": 7,
    "likelihood_reasoning": "short explanation for the score"
  }}
}}"""


def run_deep_analysis(
    prospect_id: str,
    ticker: str,
    company_name: str,
    sector: str,
    existing_signals: list,
    api_key: str,
) -> dict:
    """
    Run Claude deep analysis for one company.

    Returns a dict with:
        validated_signals: list of {index, confirmed, reasoning}
        new_signals:       list of new signal dicts
        refined_profile:   dict with strategic_direction, tailwind, headwind, likelihood
        tokens_used:       int
        announcements:     list of fetched announcements (for logging)
        error:             str (only if something went wrong)
    """
    import anthropic

    # Fetch fresh announcements
    with ASXFetcher() as fetcher:
        announcements = fetcher.fetch_announcements(ticker)

    if not announcements:
        return {"error": f"No announcements available for {ticker}"}

    prompt = _build_prompt(company_name, ticker, sector, existing_signals, announcements)

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
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

    # Parse JSON — Claude usually returns clean JSON but strip fences if present
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Try to pull the outermost { ... } block
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
