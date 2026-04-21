"""
v3_intelligence.py - Firecrawl-backed document intelligence foundation.

This module upgrades the existing headline-only analysis flow by:
1. Collecting full announcement/report content for a prospect.
2. Storing that content for reuse.
3. Running a richer Claude analysis over the full document text.

The first V3 slice intentionally stays close to the current app:
- Reuses the existing Deep Analysis button/path.
- Pulls candidate URLs from the current ASX announcement feed plus saved signals.
- Falls back cleanly to the legacy headline-only analysis when Firecrawl
  is not configured or document collection fails.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from html import unescape
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from psycopg2.extras import RealDictCursor

from asx_browser import ASXFetcher

logger = logging.getLogger("v3_intelligence")

FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
DEFAULT_DOCUMENT_PACK = 6
SMART_DOCUMENT_PACK_CAP = 12
UI_DOCUMENT_PACK_CAP = 6
MAX_DOC_CHARS = 9000
MAX_TOTAL_DOC_CHARS = 42000
ANALYSIS_VERSION = "claude-deep-v2-firecrawl"
WEBSITE_DISCOVERY_TIMEOUT = 12.0
WEBSITE_DISCOVERY_LINK_LIMIT = 24

WEBSITE_SEED_PATHS = (
    "/investors",
    "/investor-relations",
    "/investor-centre",
    "/investor-center",
    "/shareholders",
    "/announcements",
    "/asx-announcements",
    "/results",
    "/results-and-reports",
    "/reports-and-results",
    "/annual-reports",
    "/financial-reports",
    "/presentations",
)

DOCUMENT_TYPE_PRIORITY = {
    "asx_query_response": 140,
    "annual_report": 130,
    "half_year_result": 122,
    "quarterly_report": 118,
    "investor_presentation": 112,
    "pdf_filing": 95,
    "investor_relations_page": 78,
    "reports_hub_page": 74,
    "announcement": 60,
}

SYSTEM_PROMPT = (
    "You are a commercial due diligence and operational performance analyst working for New Delta, "
    "an Australian consulting firm focused on mining, energy, and heavy industry. "
    "You are reviewing full ASX filings and investor materials, not just headlines. "
    "Your job is to identify the gap between what the company said it would do and what it actually "
    "delivered, estimate the value of that gap where possible, map it to New Delta's six pillars, "
    "and produce BD-ready intelligence. Be specific, practical, and conservative when evidence is thin."
)


def get_firecrawl_api_key() -> Optional[str]:
    key = (os.getenv("FIRECRAWL_API_KEY") or "").strip()
    return key if key.startswith("fc-") else None


def firecrawl_is_configured() -> bool:
    return bool(get_firecrawl_api_key())


def get_firecrawl_status() -> dict:
    key = get_firecrawl_api_key()
    return {
        "configured": bool(key),
        "valid": bool(key),
        "source": "env" if key else None,
    }


def _parse_iso_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value)[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def _classify_document_type(title: str, url: str) -> str:
    haystack = f"{title} {url}".lower()
    if re.search(r"\b(asx\s+query|price\s+query|aware\s+letter|response\s+to\s+asx|response\s+to\s+query)\b", haystack):
        return "asx_query_response"
    if re.search(r"\b(quarter|quarterly|4c|activities report|operations review|production report|cashflow report)\b", haystack):
        return "quarterly_report"
    if re.search(r"\b(half[\s-]?year|half[\s-]?yearly|interim|hy\b|4d)\b", haystack):
        return "half_year_result"
    if re.search(r"\b(annual|full[\s-]?year|4e|annual financial report)\b", haystack):
        return "annual_report"
    if re.search(r"\b(presentation|investor|analyst|webcast)\b", haystack):
        return "investor_presentation"
    if re.search(r"\.(pdf|docx?|xlsx?|pptx?)($|\?)", haystack):
        return "pdf_filing"
    if re.search(r"\b(investor|shareholder)\b", haystack):
        return "investor_relations_page"
    if re.search(r"\b(results|reports|announcements|presentations)\b", haystack):
        return "reports_hub_page"
    if haystack.endswith(".pdf"):
        return "pdf_filing"
    return "announcement"


def _normalize_public_url(url: str) -> Optional[str]:
    raw = (url or "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    path = parsed.path or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def _normalize_candidate(
    url: str,
    title: str = "",
    date: Optional[str] = None,
    discovery_source: str = "asx_announcements",
) -> Optional[dict]:
    normalized_url = _normalize_public_url(url)
    if not normalized_url:
        return None
    return {
        "source_url": normalized_url,
        "source_title": (title or "").strip(),
        "source_date": str(date)[:10] if date else None,
        "document_type": _classify_document_type(title or "", normalized_url),
        "discovery_sources": [discovery_source],
    }


def _candidate_priority(candidate: dict) -> int:
    title = candidate.get("source_title") or ""
    url = candidate.get("source_url") or ""
    haystack = f"{title} {url}".lower()
    score = DOCUMENT_TYPE_PRIORITY.get(candidate.get("document_type") or "announcement", 50)

    boosts = (
        ("annual report", 12),
        ("full year results", 10),
        ("half year", 10),
        ("interim result", 9),
        ("quarterly", 8),
        ("activities report", 8),
        ("production report", 8),
        ("investor presentation", 7),
        ("results presentation", 7),
        ("asx query", 14),
        ("aware letter", 14),
        ("response to asx", 14),
        ("response to query", 14),
    )
    for needle, boost in boosts:
        if needle in haystack:
            score += boost

    if re.search(r"\.(pdf|docx?|xlsx?|pptx?)($|\?)", haystack):
        score += 4

    source_date = _parse_iso_date(candidate.get("source_date"))
    if source_date:
        age_days = max((datetime.utcnow() - source_date).days, 0)
        score += max(0, 20 - min(age_days // 30, 20))

    discovery_sources = candidate.get("discovery_sources") or []
    if "company_website" in discovery_sources:
        score += 2
    if "pressure_signal" in discovery_sources:
        score += 2

    return score


def _candidate_sort_key(candidate: dict) -> tuple:
    return (
        _candidate_priority(candidate),
        _parse_iso_date(candidate.get("source_date")) or datetime.min,
        candidate.get("source_title") or "",
    )


def _clean_link_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_same_site(url: str, site_domain: str) -> bool:
    domain = (urlparse(url).netloc or "").lower().lstrip("www.")
    return bool(domain) and (domain == site_domain or domain.endswith(f".{site_domain}"))


def _looks_like_relevant_site_link(url: str, title: str) -> bool:
    haystack = f"{title} {url}".lower()
    if re.search(r"\.(pdf|docx?|xlsx?|pptx?)($|\?)", haystack):
        return True
    return bool(
        re.search(
            r"\b("
            r"investor|shareholder|announcement|asx|result|report|presentation|"
            r"quarter|quarterly|half[\s-]?year|annual|financial|aware letter|query|response"
            r")\b",
            haystack,
        )
    )


def _looks_like_landing_page(url: str, title: str) -> bool:
    haystack = f"{title} {url}".lower()
    return bool(
        re.search(
            r"\b(investor|shareholder|announcement|asx|result|report|presentation|financial)\b",
            haystack,
        )
    )


def _extract_html_links(html: str, base_url: str) -> list[dict]:
    links: list[dict] = []
    for href, label in re.findall(
        r"<a\b[^>]*href\s*=\s*['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
        html or "",
        flags=re.IGNORECASE | re.DOTALL,
    ):
        raw_href = unescape((href or "").strip())
        if not raw_href or raw_href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        normalized_url = _normalize_public_url(urljoin(base_url, raw_href))
        if not normalized_url:
            continue
        links.append({
            "url": normalized_url,
            "title": _clean_link_text(label) or _clean_link_text(raw_href.rsplit("/", 1)[-1]),
        })
    return links


def _fetch_html_page(client: httpx.Client, url: str) -> Optional[tuple[str, str]]:
    try:
        response = client.get(url, follow_redirects=True)
    except Exception:
        return None
    if response.status_code >= 400:
        return None
    content_type = (response.headers.get("content-type") or "").lower()
    if "html" not in content_type:
        return None
    return (str(response.url), response.text or "")


def _discover_company_site_candidates(website: Optional[str]) -> list[dict]:
    root_url = _normalize_public_url(website or "")
    if not root_url:
        return []

    site_domain = (urlparse(root_url).netloc or "").lower().lstrip("www.")
    discovered: dict[str, dict] = {}

    def add_site_candidate(url: str, title: str = "") -> None:
        if not _is_same_site(url, site_domain):
            return
        if not _looks_like_relevant_site_link(url, title):
            return
        candidate = _normalize_candidate(
            url=url,
            title=title,
            date=None,
            discovery_source="company_website",
        )
        if not candidate:
            return
        existing = discovered.get(candidate["source_url"])
        if existing:
            if len(candidate["source_title"]) > len(existing.get("source_title") or ""):
                existing["source_title"] = candidate["source_title"]
            existing["document_type"] = (
                candidate["document_type"]
                if _candidate_priority(candidate) > _candidate_priority(existing)
                else existing["document_type"]
            )
            return
        discovered[candidate["source_url"]] = candidate

    with httpx.Client(
        timeout=WEBSITE_DISCOVERY_TIMEOUT,
        headers={"User-Agent": "Delta Prospect Intelligence/3.0"},
    ) as client:
        homepage = _fetch_html_page(client, root_url)
        pages_to_scan: list[str] = []

        if homepage:
            homepage_url, homepage_html = homepage
            for link in _extract_html_links(homepage_html, homepage_url):
                if _is_same_site(link["url"], site_domain) and _looks_like_landing_page(link["url"], link["title"]):
                    pages_to_scan.append(link["url"])
                    add_site_candidate(link["url"], link["title"])
                elif _is_same_site(link["url"], site_domain):
                    add_site_candidate(link["url"], link["title"])

        for seed_path in WEBSITE_SEED_PATHS:
            seeded = _normalize_public_url(urljoin(root_url, seed_path))
            if seeded:
                pages_to_scan.append(seeded)

        seen_pages: set[str] = set()
        for page_url in pages_to_scan:
            if page_url in seen_pages or len(seen_pages) >= 5:
                continue
            seen_pages.add(page_url)
            page = _fetch_html_page(client, page_url)
            if not page:
                continue
            resolved_url, html = page
            if _looks_like_landing_page(resolved_url, resolved_url):
                add_site_candidate(resolved_url, resolved_url.rsplit("/", 1)[-1].replace("-", " ").strip("/"))
            for link in _extract_html_links(html, resolved_url):
                if not _is_same_site(link["url"], site_domain):
                    continue
                add_site_candidate(link["url"], link["title"])
                if len(discovered) >= WEBSITE_DISCOVERY_LINK_LIMIT:
                    break
            if len(discovered) >= WEBSITE_DISCOVERY_LINK_LIMIT:
                break

    candidates = list(discovered.values())
    candidates.sort(key=_candidate_sort_key, reverse=True)
    return candidates[:WEBSITE_DISCOVERY_LINK_LIMIT]


def _merge_candidate(existing: dict, candidate: dict) -> dict:
    merged = dict(existing)
    if candidate.get("source_title") and len(candidate["source_title"]) > len(merged.get("source_title") or ""):
        merged["source_title"] = candidate["source_title"]
    existing_date = _parse_iso_date(merged.get("source_date"))
    candidate_date = _parse_iso_date(candidate.get("source_date"))
    if candidate_date and (not existing_date or candidate_date > existing_date):
        merged["source_date"] = candidate["source_date"]
    merged_sources = set(merged.get("discovery_sources") or [])
    merged_sources.update(candidate.get("discovery_sources") or [])
    merged["discovery_sources"] = sorted(merged_sources)
    if _candidate_priority(candidate) > _candidate_priority(merged):
        merged["document_type"] = candidate.get("document_type") or merged.get("document_type")
    return merged


def _resolve_document_pack_size(
    requested_limit: int,
    prospect_context: dict,
    existing_signals: list[dict],
    allow_expansion: bool = True,
) -> int:
    requested = max(1, min(int(requested_limit or DEFAULT_DOCUMENT_PACK), 20))
    if not allow_expansion or requested != DEFAULT_DOCUMENT_PACK:
        return requested

    bonus = 0
    lead_tier = (prospect_context.get("lead_tier") or "").lower()
    prospect_score = float(prospect_context.get("prospect_score") or 0)
    likelihood_score = int(prospect_context.get("likelihood_score") or 0)
    size_of_prize = int(prospect_context.get("size_of_prize") or 0)
    strong_count = sum(1 for signal in existing_signals if (signal.get("strength") or "").lower() == "strong")

    if lead_tier == "hot":
        bonus += 3
    elif lead_tier == "warm":
        bonus += 2

    if prospect_score >= 8:
        bonus += 2
    elif prospect_score >= 6.5:
        bonus += 1

    if likelihood_score >= 8:
        bonus += 1

    if strong_count >= 3:
        bonus += 2
    elif strong_count >= 1:
        bonus += 1

    if size_of_prize >= 5_000_000:
        bonus += 1

    if prospect_context.get("is_watchlisted"):
        bonus += 1

    return min(SMART_DOCUMENT_PACK_CAP, requested + bonus)


def _dedupe_candidates(
    recent_announcements: list[dict],
    existing_signals: list[dict],
    website_candidates: list[dict],
    limit: int,
) -> list[dict]:
    seen: dict[str, dict] = {}

    for ann in recent_announcements:
        candidate = _normalize_candidate(
            ann.get("url", ""),
            ann.get("title", ""),
            ann.get("date"),
            discovery_source="asx_announcements",
        )
        if candidate:
            seen[candidate["source_url"]] = _merge_candidate(seen.get(candidate["source_url"], candidate), candidate)

    for sig in existing_signals:
        candidate = _normalize_candidate(
            sig.get("source_url", ""),
            sig.get("source_title", ""),
            sig.get("source_date"),
            discovery_source="pressure_signal",
        )
        if candidate:
            seen[candidate["source_url"]] = _merge_candidate(seen.get(candidate["source_url"], candidate), candidate)

    for candidate in website_candidates:
        if candidate:
            seen[candidate["source_url"]] = _merge_candidate(seen.get(candidate["source_url"], candidate), candidate)

    candidates = list(seen.values())
    candidates.sort(key=_candidate_sort_key, reverse=True)
    return candidates[:limit]


def _scrape_with_firecrawl(url: str, api_key: str) -> dict:
    payload = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "timeout": 120000,
        "parsers": ["pdf"],
        "waitFor": 1500,
        "location": {
            "country": "AU",
            "languages": ["en-AU"],
        },
        "removeBase64Images": True,
        "storeInCache": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=130.0) as client:
        response = client.post(FIRECRAWL_SCRAPE_URL, headers=headers, json=payload)

    if response.status_code != 200:
        body = response.text[:300]
        raise RuntimeError(f"Firecrawl HTTP {response.status_code}: {body}")

    data = response.json()
    if not data.get("success"):
        raise RuntimeError(data.get("error") or "Firecrawl scrape failed")

    return data.get("data") or {}


def _markdown_to_text(markdown: str) -> str:
    text = markdown or ""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[#>*`_~\-]{1,}", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def collect_full_documents(
    conn,
    listing_id: str,
    ticker: str,
    existing_signals: list[dict],
    max_documents: int = 6,
    force_refresh: bool = False,
    allow_smart_expansion: bool = True,
    progress_callback: Optional[Callable[..., None]] = None,
) -> dict:
    api_key = get_firecrawl_api_key()
    if not api_key:
        return {
            "error": "Firecrawl API key not configured",
            "documents": [],
            "requested": 0,
            "fetched": 0,
            "reused": 0,
            "failed": 0,
        }

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT l.website,
                   pm.prospect_score,
                   pm.likelihood_score,
                   pm.lead_tier,
                   pm.size_of_prize,
                   pm.is_watchlisted
            FROM asx_listings l
            LEFT JOIN prospect_matrix pm ON pm.listing_id = l.id
            WHERE l.id = %s
            """,
            (listing_id,),
        )
        prospect_context = dict(cur.fetchone() or {})

    effective_limit = _resolve_document_pack_size(
        max_documents,
        prospect_context,
        existing_signals,
        allow_expansion=allow_smart_expansion,
    )
    if progress_callback:
        progress_callback(
            progress_pct=18,
            stage="collecting_sources",
            message="Gathering ASX and company website documents",
        )

    with ASXFetcher() as fetcher:
        recent_announcements = fetcher.fetch_announcements(ticker)

    website_candidates = _discover_company_site_candidates(prospect_context.get("website"))
    candidates = _dedupe_candidates(
        recent_announcements=recent_announcements,
        existing_signals=existing_signals,
        website_candidates=website_candidates,
        limit=effective_limit,
    )
    if not candidates:
        return {
            "documents": [],
            "requested": 0,
            "fetched": 0,
            "reused": 0,
            "failed": 0,
            "effective_max_documents": effective_limit,
        }

    documents: list[dict] = []
    fetched = 0
    reused = 0
    failed = 0

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        total_candidates = len(candidates)
        for idx, candidate in enumerate(candidates, start=1):
            url = candidate["source_url"]
            cur.execute(
                """
                SELECT id, source_url, source_title, source_date, document_type,
                       provider, fetch_status, content_markdown, content_text,
                       metadata, content_hash, last_error, fetched_at, created_at, updated_at
                FROM announcement_documents
                WHERE listing_id = %s AND source_url = %s
                """,
                (listing_id, url),
            )
            existing = cur.fetchone()

            if existing and existing["fetch_status"] == "success" and existing.get("content_text") and not force_refresh:
                documents.append(dict(existing))
                reused += 1
                if progress_callback:
                    progress_callback(
                        progress_pct=20 + int((idx / total_candidates) * 35),
                        stage="collecting_documents",
                        message=f"Reusing stored document {idx} of {total_candidates}",
                    )
                continue

            try:
                scraped = _scrape_with_firecrawl(url, api_key)
                markdown = (scraped.get("markdown") or "").strip()
                text = _markdown_to_text(markdown)
                metadata = scraped.get("metadata") or {}
                if not isinstance(metadata, dict):
                    metadata = {"raw_metadata": metadata}
                metadata["_delta"] = {
                    "discovery_sources": candidate.get("discovery_sources") or [],
                    "candidate_priority": _candidate_priority(candidate),
                }
                content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest() if markdown else None

                cur.execute(
                    """
                    INSERT INTO announcement_documents
                        (listing_id, source_url, source_title, source_date, document_type,
                         provider, fetch_status, content_markdown, content_text, metadata,
                         content_hash, last_error, fetched_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,'firecrawl','success',%s,%s,%s,%s,NULL,NOW(),NOW())
                    ON CONFLICT (listing_id, source_url) DO UPDATE SET
                        source_title = EXCLUDED.source_title,
                        source_date = EXCLUDED.source_date,
                        document_type = EXCLUDED.document_type,
                        provider = EXCLUDED.provider,
                        fetch_status = EXCLUDED.fetch_status,
                        content_markdown = EXCLUDED.content_markdown,
                        content_text = EXCLUDED.content_text,
                        metadata = EXCLUDED.metadata,
                        content_hash = EXCLUDED.content_hash,
                        last_error = NULL,
                        fetched_at = NOW(),
                        updated_at = NOW()
                    RETURNING id, source_url, source_title, source_date, document_type,
                              provider, fetch_status, content_markdown, content_text,
                              metadata, content_hash, last_error, fetched_at, created_at, updated_at
                    """,
                    (
                        listing_id,
                        url,
                        candidate["source_title"] or metadata.get("title"),
                        candidate["source_date"],
                        candidate["document_type"],
                        markdown,
                        text,
                        json.dumps(metadata),
                        content_hash,
                    ),
                )
                row = cur.fetchone()
                documents.append(dict(row))
                fetched += 1
                if progress_callback:
                    progress_callback(
                        progress_pct=20 + int((idx / total_candidates) * 35),
                        stage="collecting_documents",
                        message=f"Fetching document {idx} of {total_candidates}",
                    )
            except Exception as exc:
                failed += 1
                logger.warning("%s: Firecrawl scrape failed for %s: %s", ticker, url, exc)
                cur.execute(
                    """
                    INSERT INTO announcement_documents
                        (listing_id, source_url, source_title, source_date, document_type,
                         provider, fetch_status, last_error, updated_at)
                    VALUES (%s,%s,%s,%s,%s,'firecrawl','failed',%s,NOW())
                    ON CONFLICT (listing_id, source_url) DO UPDATE SET
                        source_title = EXCLUDED.source_title,
                        source_date = EXCLUDED.source_date,
                        document_type = EXCLUDED.document_type,
                        provider = EXCLUDED.provider,
                        fetch_status = EXCLUDED.fetch_status,
                        last_error = EXCLUDED.last_error,
                        updated_at = NOW()
                    """,
                    (
                        listing_id,
                        url,
                        candidate["source_title"],
                        candidate["source_date"],
                        candidate["document_type"],
                        str(exc)[:500],
                    ),
                )
                if progress_callback:
                    progress_callback(
                        progress_pct=20 + int((idx / total_candidates) * 35),
                        stage="collecting_documents",
                        message=f"Skipping failed document {idx} of {total_candidates}",
                    )

    conn.commit()
    documents.sort(key=_candidate_sort_key, reverse=True)
    if progress_callback:
        progress_callback(
            progress_pct=58,
            stage="documents_ready",
            message=f"Prepared {len(documents)} documents for analysis",
        )
    return {
        "documents": documents,
        "requested": len(candidates),
        "fetched": fetched,
        "reused": reused,
        "failed": failed,
        "effective_max_documents": effective_limit,
    }


def _build_v3_prompt(
    company_name: str,
    ticker: str,
    sector: str,
    existing_signals: list[dict],
    documents: list[dict],
    size_of_prize: int = 0,
    deal_fit: str = "",
) -> str:
    signal_lines = "\n".join(
        f"  [{i}] {s.get('pressure_type', '?')}/{s.get('strength', '?')}: {s.get('summary', '')}"
        for i, s in enumerate(existing_signals)
    ) or "  (none)"

    doc_blocks = []
    total_chars = 0
    for idx, doc in enumerate(documents, start=1):
        content = (doc.get("content_text") or doc.get("content_markdown") or "").strip()
        if not content:
            continue
        remaining = MAX_TOTAL_DOC_CHARS - total_chars
        if remaining <= 0:
            break
        snippet = content[: min(MAX_DOC_CHARS, remaining)]
        total_chars += len(snippet)
        doc_blocks.append(
            f"[DOCUMENT {idx}] {doc.get('source_title') or 'Untitled'}\n"
            f"Date: {doc.get('source_date') or 'Unknown'}\n"
            f"Type: {doc.get('document_type') or 'announcement'}\n"
            f"URL: {doc.get('source_url')}\n"
            f"Content:\n{snippet}"
        )

    prize_str = (
        f"${size_of_prize/1_000_000:.1f}M" if size_of_prize >= 1_000_000 else
        f"${size_of_prize/1_000:.0f}K" if size_of_prize > 0 else
        "Not calculated"
    )

    return f"""You are reviewing {company_name} ({ticker}), an ASX-listed {sector} company, for New Delta.

CURRENT RULE-BASED SIGNALS:
{signal_lines}

CURRENT HEURISTIC SIZE OF PRIZE: {prize_str}
CURRENT DEAL FIT: {deal_fit or 'Not classified'}

FULL SOURCE DOCUMENTS:
{chr(10).join(doc_blocks) if doc_blocks else '(none)'}

Respond with valid JSON only. No markdown fences. No text outside the JSON.

{{
  "validated_signals": [
    {{"index": 0, "confirmed": true, "reasoning": "short reason"}}
  ],
  "new_signals": [
    {{
      "pressure_type": "production|license_to_operate|cost|people|quality|future_readiness",
      "strength": "strong|moderate|weak",
      "summary": "one-sentence pressure description",
      "reasoning": "what in the documents led to this",
      "source_title": "document title"
    }}
  ],
  "refined_profile": {{
    "strategic_direction": "1-2 sentence plain-English description of what management is trying to do",
    "key_pressures": "2-4 sentences on the biggest operational or financial pressures using evidence from the docs",
    "nd_fit_assessment": "2-4 sentences on where New Delta can help most and why",
    "primary_tailwind": "single strongest tailwind",
    "primary_headwind": "single strongest headwind",
    "likelihood_score": 7,
    "likelihood_reasoning": "why the urgency is or is not real"
  }},
  "gap_findings": [
    {{
      "metric": "production|cost|revenue|capex|schedule|safety|other",
      "guided_value": "the target / guidance / expectation if stated, else null",
      "actual_value": "the actual delivered number if stated, else null",
      "gap_description": "the difference in plain English",
      "estimated_value_impact": "dollar impact estimate or null if not defensible",
      "pillar": "production|license_to_operate|cost|people|quality|future_readiness",
      "confidence": "high|medium|low",
      "source_title": "document title",
      "reasoning": "short explanation"
    }}
  ],
  "executive_summary": "3-5 sentence summary of the opportunity in commercial language",
  "prize_assessment": "Is the current {prize_str} heuristic too low, too high, or directionally right? Explain briefly.",
  "outreach_hypothesis": "2-4 sentences a salesperson could actually use in outreach, grounded in the evidence.",
  "red_flags": "Any reasons not to pursue, or null if none."
}}"""


def run_full_document_analysis(
    prospect_id: str,
    ticker: str,
    company_name: str,
    sector: str,
    existing_signals: list[dict],
    documents: list[dict],
    api_key: str,
    size_of_prize: int = 0,
    deal_fit: str = "",
    progress_callback: Optional[Callable[..., None]] = None,
) -> dict:
    if not documents:
        return {"error": f"No stored documents available for {ticker}"}

    import anthropic

    prompt = _build_v3_prompt(
        company_name=company_name,
        ticker=ticker,
        sector=sector,
        existing_signals=existing_signals,
        documents=documents,
        size_of_prize=size_of_prize,
        deal_fit=deal_fit,
    )
    if progress_callback:
        progress_callback(
            progress_pct=66,
            stage="preparing_prompt",
            message=f"Preparing AI brief from {len(documents)} documents",
        )

    client = anthropic.Anthropic(api_key=api_key)
    try:
        if progress_callback:
            progress_callback(
                progress_pct=74,
                stage="running_ai",
                message="Running AI analysis on the evidence pack",
            )
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        return {"error": "Invalid Anthropic API key - please check Settings"}
    except anthropic.RateLimitError:
        return {"error": "Anthropic rate limit hit - try again in a moment"}
    except Exception as exc:
        return {"error": f"Claude API error: {exc}"}

    tokens_used = response.usage.input_tokens + response.usage.output_tokens
    if progress_callback:
        progress_callback(
            progress_pct=86,
            stage="parsing_ai_output",
            message="Reading the AI response",
        )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {
                "error": f"Claude returned non-JSON response: {raw[:300]}",
                "error_code": "non_json_response",
            }
        try:
            result = json.loads(match.group())
        except json.JSONDecodeError:
            return {
                "error": f"Claude returned non-JSON response: {raw[:300]}",
                "error_code": "non_json_response",
            }

    result["tokens_used"] = tokens_used
    result["announcements"] = [
        {
            "title": doc.get("source_title"),
            "url": doc.get("source_url"),
            "date": str(doc.get("source_date") or "")[:10],
        }
        for doc in documents
    ]
    result["documents_used"] = len(documents)
    result["analysis_mode"] = "full_documents"
    result["analysis_version"] = ANALYSIS_VERSION
    result["model_name"] = "claude-sonnet-4-6"
    logger.info("%s: V3 full-document analysis complete using %d documents", ticker, len(documents))
    return result


def store_intelligence_run(
    conn,
    prospect_id: str,
    listing_id: str,
    result: dict,
) -> None:
    summary = result.get("executive_summary") or result.get("prize_assessment") or result.get("outreach_hypothesis")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO prospect_intelligence_runs
                (prospect_id, listing_id, analysis_type, provider, model_name,
                 status, source_count, summary, output_json, completed_at)
            VALUES (%s,%s,%s,'anthropic',%s,'completed',%s,%s,%s,NOW())
            """,
            (
                prospect_id,
                listing_id,
                result.get("analysis_version") or ANALYSIS_VERSION,
                result.get("model_name") or "claude-sonnet-4-6",
                int(result.get("documents_used") or len(result.get("announcements") or [])),
                summary,
                json.dumps(result),
            ),
        )
    conn.commit()
