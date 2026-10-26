"""Contact discovery from saved snapshots, Hunter, Apollo, and public websites."""

import hashlib
import html
import logging
import os
import re
from io import BytesIO
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from psycopg2.extras import Json, RealDictCursor

import safe_http


logger = logging.getLogger("contact_enrichment")
# httpx logs full query strings at INFO level. Hunter authenticates via an
# api_key query parameter, so INFO request logging would leak credentials.
logging.getLogger("httpx").setLevel(logging.WARNING)

APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
APOLLO_ENRICH_URL = "https://api.apollo.io/api/v1/people/match"
HUNTER_DOMAIN_URL = "https://api.hunter.io/v2/domain-search"
CONTACTS_PER_COMPANY = 2
PUBLIC_CONTACT_HINTS = ("contact", "about", "team", "people", "leadership", "investor", "corporate")
PUBLIC_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+(?![\w.-])")
IGNORED_EMAIL_DOMAINS = {
    "example.com", "sentry.io", "wixpress.com", "wordpress.org",
    "cloudflare.com", "google.com", "asx.com.au", "nzx.com",
}
GENERIC_EMAIL_NAMES = {
    "admin", "contact", "corporate", "enquiries", "enquiry", "hello", "info",
    "investor", "investors", "ir", "media", "office", "support",
}

TARGET_TITLES = [
    "Chief Operating Officer",
    "COO",
    "Executive General Manager Operations",
    "General Manager Operations",
    "Vice President Operations",
    "VP Operations",
    "Head of Operations",
    "Operations Director",
    "Director of Operations",
    "Chief Technical Officer",
    "Technical Director",
    "General Manager Technical Services",
    "Head of Technical Services",
    "General Manager Projects",
    "Head of Projects",
    "General Manager Processing",
    "Processing Manager",
    "Asset Manager",
    "Mine Manager",
    "General Manager Procurement",
    "Head of Procurement",
]


class ContactProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str = "provider_error"):
        super().__init__(message)
        self.code = code


def company_domain(website: str | None) -> str | None:
    if not website:
        return None
    value = website.strip()
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def normalize_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def title_score(title: str | None, seniority: str | None = None) -> int:
    text = (title or "").lower()
    score = 10
    weights = (
        ("chief operating", 100), ("coo", 100),
        ("operations", 94), ("operational", 94),
        ("technical services", 92), ("chief technical", 92),
        ("processing", 90), ("projects", 88),
        ("procurement", 86), ("supply chain", 84),
        ("asset manager", 82), ("mine manager", 82),
        ("managing director", 80), ("chief executive", 80), ("ceo", 80),
        ("chief financial", 74), ("cfo", 74),
        ("vice president", 70), ("vp ", 70),
        ("general manager", 68), ("head of", 64),
        ("director", 60), ("manager", 50),
    )
    for needle, value in weights:
        if needle in text:
            score = max(score, value)
    seniority_bonus = {
        "c_suite": 8, "owner": 8, "vp": 6, "head": 5,
        "director": 4, "manager": 2, "executive": 8, "senior": 3,
    }
    return min(100, score + seniority_bonus.get((seniority or "").lower(), 0))


PROVIDER_RETRIES = 2


def _request_json(method: str, url: str, *, headers=None, params=None, payload=None, timeout=30, retries: int = PROVIDER_RETRIES):
    """Call a contact provider. Transient failures (timeouts, 5xx) are retried with
    jittered backoff; 429 honours Retry-After once, then surfaces as rate_limited."""
    import random
    import time as _time

    attempt = 0
    while True:
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.request(method, url, headers=headers, params=params, json=payload)
        except httpx.HTTPError as exc:
            if attempt >= retries:
                raise ContactProviderError(f"request failed: {exc}") from exc
            _time.sleep(min(2 ** attempt, 6) + random.uniform(0, 0.5))
            attempt += 1
            continue
        if response.status_code == 429 and attempt < retries:
            _time.sleep(safe_http.retry_after_seconds(response.headers.get("retry-after"), default=3.0, cap=20.0))
            attempt += 1
            continue
        if response.status_code >= 500 and attempt < retries:
            _time.sleep(min(2 ** attempt, 6) + random.uniform(0, 0.5))
            attempt += 1
            continue
        break
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            detail = body.get("error") or body.get("message") or body.get("detail") or ""
            if isinstance(detail, dict):
                detail = detail.get("message", "")
        except Exception:
            detail = response.text[:200]
        detail = str(detail or "provider rejected request")
        detail_lower = detail.lower()
        if "insufficient credits" in detail_lower or "credit limit" in detail_lower:
            raise ContactProviderError("credits exhausted", code="credits_exhausted")
        if response.status_code == 429:
            raise ContactProviderError("rate limit reached", code="rate_limited")
        raise ContactProviderError(f"HTTP {response.status_code}: {detail[:240]}")
    return response.json()


VERIFIED_EMAIL_STATUSES = {"valid", "verified", "deliverable", "available", "accept_all"}
UNVERIFIED_EMAIL_STATUSES = {"unverified", "unknown", "risky", None, ""}


def classify_contact(contact: dict) -> str:
    """Provenance label shown next to a contact so nobody mistakes a generic
    inbox for a named, verified decision maker.

    generic_inbox     — info@/investors@ style address, no person behind it
    named_inferred    — person name guessed from an email address or filing text
    named_verified    — provider-verified or snapshot-sourced person with usable email
    named_unverified  — real person, email missing or not yet verified
    """
    email = (contact.get("email") or "").lower()
    local = email.split("@", 1)[0] if email else ""
    provider = (contact.get("source_provider") or "").lower()
    name = (contact.get("full_name") or "").strip()
    raw = contact.get("raw_data") or {}
    if local in GENERIC_EMAIL_NAMES or (provider == "public_website" and (not name or name == contact.get("title"))):
        return "generic_inbox"
    if isinstance(raw, dict) and (raw.get("name_inferred") or raw.get("inferred_from_email")):
        return "named_inferred"
    status = (contact.get("email_status") or "").lower() or None
    if email and (status in VERIFIED_EMAIL_STATUSES or provider in ("snapshot", "apollo", "hunter") and status not in UNVERIFIED_EMAIL_STATUSES):
        return "named_verified"
    return "named_unverified"


def snapshot_contact_records(snapshot_data: dict | None) -> list[dict]:
    """Normalize management contacts already stored in a PitchBook snapshot."""
    records = []
    contacts = (snapshot_data or {}).get("management_contacts") or []
    if not isinstance(contacts, list):
        return records

    for raw in contacts:
        if not isinstance(raw, dict):
            continue
        full_name = (raw.get("name") or raw.get("full_name") or "").strip()
        title = (raw.get("title") or raw.get("role") or "").strip() or None
        email = (raw.get("email") or "").strip().lower() or None
        phone = (raw.get("phone") or "").strip() or None
        linkedin_url = (raw.get("linkedin_url") or raw.get("linkedin") or "").strip() or None
        if not any((full_name, title, email, phone, linkedin_url)):
            continue
        identity = email or hashlib.sha256(
            f"{normalize_name(full_name)}:{(title or '').lower()}:{phone or ''}".encode("utf-8")
        ).hexdigest()[:24]
        records.append({
            "source_provider": "snapshot",
            "source_contact_id": identity,
            "full_name": full_name or "Company contact",
            "title": title,
            "seniority": None,
            "departments": [],
            "email": email,
            "email_status": "available" if email else "missing",
            "phone": phone,
            "linkedin_url": linkedin_url,
            "confidence_score": 0.95 if email else 0.80,
            "raw_data": raw,
        })
    return records


def sync_snapshot_contacts(conn, prospect_id: str | None = None) -> dict:
    """Idempotently copy snapshot management contacts into prospect_contacts."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if prospect_id:
            cur.execute(
                "SELECT prospect_id, snapshot_data FROM pitchbook_snapshots WHERE prospect_id = %s",
                (prospect_id,),
            )
        else:
            cur.execute("SELECT prospect_id, snapshot_data FROM pitchbook_snapshots")
        snapshots = list(cur.fetchall())

    stats = {"snapshots": len(snapshots), "added": 0, "updated": 0}
    for snapshot in snapshots:
        for contact in snapshot_contact_records(snapshot.get("snapshot_data")):
            _, inserted = save_contact(conn, str(snapshot["prospect_id"]), contact)
            stats["added" if inserted else "updated"] += 1
    return stats


def _public_emails(page: str) -> list[str]:
    found = []
    for email in PUBLIC_EMAIL_RE.findall(html.unescape(page or "")):
        email = email.lower().strip(".,;:!?)]}\"'")
        domain = email.rsplit("@", 1)[-1]
        if domain in IGNORED_EMAIL_DOMAINS or email in found:
            continue
        if email.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")):
            continue
        found.append(email)
    return found


def _public_contact_title(email: str) -> str:
    local = email.split("@", 1)[0].lower().replace(".", "").replace("_", "")
    if "investor" in local or local in {"ir", "investors"}:
        return "Investor relations"
    if "media" in local or "press" in local:
        return "Media contact"
    if "admin" in local or "office" in local:
        return "Administration"
    return "General company contact"


def public_website_contacts(website: str, *, limit: int = CONTACTS_PER_COMPANY) -> list[dict]:
    """Find public company inboxes on the official site as a zero-cost fallback."""
    base_url = website.strip()
    if not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
    base_host = (urlparse(base_url).hostname or "").lower()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DeltaProspectContactCheck/1.0)"}
    pages: list[tuple[str, str]] = []

    try:
        with httpx.Client(timeout=15, follow_redirects=False, headers=headers) as client:
            homepage_response = safe_http.fetch(base_url, client=client, retries=1)
            homepage = homepage_response.text
            pages.append((str(homepage_response.url), homepage))

            candidate_urls = []
            for href in re.findall(r'''href=["']([^"'#?]+)["']''', homepage, flags=re.I):
                absolute = urljoin(str(homepage_response.url), html.unescape(href))
                parsed = urlparse(absolute)
                if (parsed.hostname or "").lower() != (urlparse(str(homepage_response.url)).hostname or "").lower():
                    continue
                if any(hint in parsed.path.lower() for hint in PUBLIC_CONTACT_HINTS):
                    if absolute not in candidate_urls:
                        candidate_urls.append(absolute)
            for url in candidate_urls[:5]:
                try:
                    response = safe_http.fetch(url, client=client, retries=0)
                    pages.append((str(response.url), response.text))
                except (safe_http.FetchError, safe_http.UnsafeURL):
                    continue
    except (safe_http.FetchError, safe_http.UnsafeURL, httpx.HTTPError) as exc:
        logger.info("public website lookup skipped for %s: %s", base_url, exc)
        return []

    discovered: dict[str, str] = {}
    for source_url, page in pages:
        for email in _public_emails(page):
            email_domain = email.rsplit("@", 1)[-1]
            # Prefer official-domain addresses. Keep external IR agencies only
            # when there are no company-domain contacts at all.
            if email_domain == base_host or email_domain.endswith(f".{base_host}"):
                discovered.setdefault(email, source_url)
    if not discovered:
        for source_url, page in pages:
            for email in _public_emails(page):
                discovered.setdefault(email, source_url)

    contacts = []
    for email, source_url in list(discovered.items())[:limit]:
        title = _public_contact_title(email)
        contacts.append({
            "source_provider": "public_website",
            "source_contact_id": email,
            "full_name": title,
            "title": title,
            "seniority": None,
            "departments": [],
            "email": email,
            "email_status": "unverified",
            "phone": None,
            "linkedin_url": None,
            "confidence_score": 0.55,
            "raw_data": {"source_url": source_url, "official_website": base_url},
        })
    return contacts


def _name_parts_from_email(email: str) -> tuple[str | None, str | None, str]:
    """Infer a display name only when an address clearly spells it out."""
    local = email.split("@", 1)[0].lower()
    parts = [part for part in re.split(r"[._-]+", local) if part]
    if len(parts) < 2 or any(part in GENERIC_EMAIL_NAMES or not part.isalpha() for part in parts):
        return None, None, _public_contact_title(email)
    first_name = parts[0].title()
    last_name = parts[-1].title()
    full_name = " ".join(part.title() for part in parts)
    return first_name, last_name, full_name


def asx_announcement_contacts(
    ticker: str,
    *,
    domain_hint: str | None = None,
    limit: int = CONTACTS_PER_COMPANY,
    max_announcements: int = 3,
) -> list[dict]:
    """Extract public contact emails from recent official ASX announcement PDFs."""
    try:
        from pypdf import PdfReader
        from asx_browser import ASXFetcher
    except ImportError:
        logger.warning("ASX contact fallback unavailable because pypdf is not installed")
        return []

    candidates: dict[str, dict] = {}
    try:
        with ASXFetcher() as fetcher:
            announcements = fetcher.fetch_announcements(ticker)[:max_announcements]
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            for announcement in announcements:
                try:
                    response = client.get(announcement["url"])
                    response.raise_for_status()
                    if len(response.content) > 12_000_000:
                        continue
                    reader = PdfReader(BytesIO(response.content))
                    page_count = len(reader.pages)
                    indexes = list(range(min(page_count, 8)))
                    indexes.extend(index for index in range(max(0, page_count - 3), page_count) if index not in indexes)
                    text = "\n".join((reader.pages[index].extract_text() or "") for index in indexes)
                except Exception as exc:
                    logger.debug("ASX contact PDF skipped for %s: %s", ticker, exc)
                    continue

                for email in _public_emails(text):
                    email_domain = email.rsplit("@", 1)[-1]
                    if email_domain in IGNORED_EMAIL_DOMAINS:
                        continue
                    score = 0
                    if domain_hint and (
                        email_domain == domain_hint
                        or email_domain.endswith(f".{domain_hint}")
                        or domain_hint.endswith(f".{email_domain}")
                    ):
                        score += 100
                    local = email.split("@", 1)[0].lower()
                    if any(token in local for token in ("investor", "companysecretary", "corporate", "media")):
                        score += 30
                    if local in {"ir", "info", "contact", "enquiries", "hello"}:
                        score += 20
                    candidate = {
                        "email": email,
                        "score": score,
                        "announcement": announcement,
                    }
                    previous = candidates.get(email)
                    if not previous or candidate["score"] > previous["score"]:
                        candidates[email] = candidate
                if len(candidates) >= limit and any(item["score"] >= 100 for item in candidates.values()):
                    break
                if len(candidates) >= max(limit * 2, 3):
                    break
    except Exception as exc:
        logger.warning("ASX announcement contact lookup failed for %s: %s", ticker, exc)
        return []

    domain_counts: dict[str, int] = {}
    for email in candidates:
        email_domain = email.rsplit("@", 1)[-1]
        domain_counts[email_domain] = domain_counts.get(email_domain, 0) + 1
    for email, candidate in candidates.items():
        candidate["score"] += min(domain_counts[email.rsplit("@", 1)[-1]], 5) * 10

    ranked = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)
    contacts = []
    for candidate in ranked[:limit]:
        email = candidate["email"]
        first_name, last_name, full_name = _name_parts_from_email(email)
        title = _public_contact_title(email)
        announcement = candidate["announcement"]
        contacts.append({
            "source_provider": "asx_announcement",
            "source_contact_id": email,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "title": title,
            "seniority": None,
            "departments": [],
            "email": email,
            "email_status": "unverified",
            "phone": None,
            "linkedin_url": None,
            "confidence_score": 0.80 if candidate["score"] >= 100 else 0.70,
            "raw_data": {
                "source_url": announcement.get("url"),
                "source_title": announcement.get("title"),
                "source_date": announcement.get("date"),
                "inferred_from_email": first_name is not None,
            },
        })
    return contacts


def apollo_candidates(domain: str, api_key: str) -> list[dict]:
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json", "Accept": "application/json"}
    base_payload = {
        "q_organization_domains_list": [domain],
        "person_seniorities": ["c_suite", "vp", "head", "director", "manager"],
        "page": 1,
        "per_page": 25,
    }
    targeted = {**base_payload, "person_titles": TARGET_TITLES}
    result = _request_json("POST", APOLLO_SEARCH_URL, headers=headers, payload=targeted)
    people = list(result.get("people") or [])

    # Add broad executives as fallback candidates. Search calls do not reveal
    # contact details and therefore do not spend enrichment credits.
    broad = {**base_payload, "per_page": 10}
    result = _request_json("POST", APOLLO_SEARCH_URL, headers=headers, payload=broad)
    seen = {person.get("id") for person in people}
    people.extend(person for person in (result.get("people") or []) if person.get("id") not in seen)
    people.sort(key=lambda p: title_score(p.get("title"), p.get("seniority")), reverse=True)
    return people


def apollo_contact(domain: str, api_key: str, exclude_ids: set[str] | None = None) -> dict | None:
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json", "Accept": "application/json"}
    excluded = exclude_ids or set()
    people = [person for person in apollo_candidates(domain, api_key) if str(person.get("id")) not in excluded]
    if not people:
        return None

    candidate = people[0]
    person_id = candidate.get("id")
    if not person_id:
        return None

    enriched = _request_json(
        "POST",
        APOLLO_ENRICH_URL,
        headers=headers,
        payload={
            "id": person_id,
            "reveal_personal_emails": False,
            "reveal_phone_number": False,
        },
    )
    person = enriched.get("person") or enriched.get("contact") or {}
    if not person:
        return None
    email = person.get("email")
    return {
        "source_provider": "apollo",
        "source_contact_id": str(person.get("id") or person_id),
        "first_name": person.get("first_name") or candidate.get("first_name"),
        "last_name": person.get("last_name") or candidate.get("last_name"),
        "full_name": person.get("name") or candidate.get("name"),
        "title": person.get("title") or candidate.get("title"),
        "seniority": person.get("seniority") or candidate.get("seniority"),
        "departments": person.get("departments") or candidate.get("departments") or [],
        "email": email,
        "email_status": person.get("email_status") or ("available" if email else "unavailable"),
        "phone": None,
        "linkedin_url": person.get("linkedin_url") or candidate.get("linkedin_url"),
        "confidence_score": 0.85 if email else 0.65,
        "raw_data": person,
    }


def _hunter_contact_record(domain: str, person: dict) -> dict:
    first_name = person.get("first_name")
    last_name = person.get("last_name")
    full_name = " ".join(part for part in (first_name, last_name) if part).strip() or None
    verification = person.get("verification") or {}
    confidence = person.get("confidence")
    return {
        "source_provider": "hunter",
        "source_contact_id": person.get("value") or hashlib.sha256(
            f"{domain}:{full_name}:{person.get('position')}".encode("utf-8")
        ).hexdigest()[:24],
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "title": person.get("position"),
        "seniority": person.get("seniority"),
        "departments": [person.get("department")] if person.get("department") else [],
        "email": person.get("value"),
        "email_status": verification.get("status") or "available",
        "phone": person.get("phone_number"),
        "linkedin_url": person.get("linkedin"),
        "confidence_score": (float(confidence) / 100) if confidence is not None else 0.75,
        "raw_data": person,
    }


def hunter_contacts(
    domain: str,
    api_key: str,
    *,
    limit: int = CONTACTS_PER_COMPANY,
    exclude_ids: set[str] | None = None,
) -> list[dict]:
    excluded = exclude_ids or set()
    params = {
        "domain": domain,
        "api_key": api_key,
        "type": "personal",
        "seniority": "executive",
        "limit": min(10, max(limit + len(excluded), CONTACTS_PER_COMPANY)),
    }
    result = _request_json("GET", HUNTER_DOMAIN_URL, params=params)
    emails = (result.get("data") or {}).get("emails") or []

    # Broaden once when executive-only results cannot fill the two-contact target.
    available_ids = {str(person.get("value") or "") for person in emails} - excluded
    if len(available_ids) < limit:
        params.pop("seniority", None)
        result = _request_json("GET", HUNTER_DOMAIN_URL, params=params)
        broad_emails = (result.get("data") or {}).get("emails") or []
        seen = {str(person.get("value") or "") for person in emails}
        emails.extend(person for person in broad_emails if str(person.get("value") or "") not in seen)

    contacts = [_hunter_contact_record(domain, person) for person in emails]
    contacts = [contact for contact in contacts if str(contact["source_contact_id"]) not in excluded]
    contacts.sort(
        key=lambda contact: title_score(contact.get("title"), contact.get("seniority")),
        reverse=True,
    )
    return contacts[:limit]


def hunter_contact(domain: str, api_key: str) -> dict | None:
    """Backward-compatible single-result wrapper."""
    contacts = hunter_contacts(domain, api_key, limit=1)
    return contacts[0] if contacts else None


def save_contact(conn, prospect_id: str, contact: dict) -> tuple[str, bool]:
    first = contact.get("first_name")
    last = contact.get("last_name")
    full_name = contact.get("full_name") or " ".join(p for p in (first, last) if p).strip() or "Unknown"
    normalized = normalize_name(full_name)
    email = (contact.get("email") or "").strip().lower() or None
    provider = contact["source_provider"]
    now = datetime.now(timezone.utc)

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if email:
            cur.execute(
                "SELECT id, source_provider FROM prospect_contacts WHERE prospect_id = %s AND LOWER(email) = %s LIMIT 1",
                (prospect_id, email),
            )
        else:
            cur.execute(
                """SELECT id, source_provider FROM prospect_contacts
                   WHERE prospect_id = %s AND normalized_name = %s AND LOWER(COALESCE(title, '')) = LOWER(%s)
                   LIMIT 1""",
                (prospect_id, normalized, contact.get("title") or ""),
            )
        existing = cur.fetchone()
        if existing:
            providers = {p for p in (existing["source_provider"] or "").split(",") if p}
            providers.add(provider)
            cur.execute(
                """UPDATE prospect_contacts SET
                       source_provider = %s,
                       full_name = CASE WHEN full_name IN ('Unknown', 'Company contact') THEN %s ELSE full_name END,
                       normalized_name = CASE WHEN full_name IN ('Unknown', 'Company contact') THEN %s ELSE normalized_name END,
                       first_name = COALESCE(first_name, %s),
                       last_name = COALESCE(last_name, %s),
                       title = COALESCE(title, %s),
                       seniority = COALESCE(seniority, %s),
                       departments = CASE WHEN CARDINALITY(departments) = 0 THEN %s ELSE departments END,
                       decision_rank = GREATEST(decision_rank, %s),
                       email = COALESCE(email, %s),
                       email_status = CASE WHEN %s IS NOT NULL THEN %s ELSE email_status END,
                       phone = COALESCE(phone, %s),
                       linkedin_url = COALESCE(linkedin_url, %s),
                       confidence_score = GREATEST(COALESCE(confidence_score, 0), %s),
                       last_verified_at = %s,
                       updated_at = %s
                   WHERE id = %s""",
                (
                    ",".join(sorted(providers)), full_name, normalized, first, last,
                    contact.get("title"), contact.get("seniority"), contact.get("departments") or [],
                    title_score(contact.get("title"), contact.get("seniority")),
                    email, email, contact.get("email_status"),
                    contact.get("phone"), contact.get("linkedin_url"),
                    contact.get("confidence_score"), now if email else None, now, existing["id"],
                ),
            )
            conn.commit()
            return str(existing["id"]), False

        cur.execute(
            """INSERT INTO prospect_contacts (
                   prospect_id, full_name, normalized_name, first_name, last_name,
                   title, seniority, departments, decision_rank, email, email_status,
                   phone, linkedin_url, source_provider, source_contact_id,
                   confidence_score, raw_data, last_verified_at
               ) VALUES (
                   %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                   %s, %s, %s, %s, %s, %s, %s
               )
               ON CONFLICT (prospect_id, source_provider, source_contact_id)
               DO UPDATE SET
                   title = EXCLUDED.title,
                   seniority = EXCLUDED.seniority,
                   departments = EXCLUDED.departments,
                   decision_rank = EXCLUDED.decision_rank,
                   email = COALESCE(EXCLUDED.email, prospect_contacts.email),
                   email_status = EXCLUDED.email_status,
                   linkedin_url = COALESCE(EXCLUDED.linkedin_url, prospect_contacts.linkedin_url),
                   confidence_score = EXCLUDED.confidence_score,
                   raw_data = EXCLUDED.raw_data,
                   last_verified_at = EXCLUDED.last_verified_at,
                   updated_at = NOW()
               RETURNING id, (xmax = 0) AS inserted""",
            (
                prospect_id, full_name, normalized, first, last,
                contact.get("title"), contact.get("seniority"), contact.get("departments") or [],
                title_score(contact.get("title"), contact.get("seniority")), email,
                contact.get("email_status"), contact.get("phone"), contact.get("linkedin_url"),
                provider, contact["source_contact_id"], contact.get("confidence_score"),
                Json(contact.get("raw_data") or {}), now if email else None,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row["id"]), bool(row["inserted"])


def enrich_company_contacts(
    conn,
    prospect: dict,
    *,
    force: bool = False,
    allow_apollo_second: bool = False,
    allow_apollo: bool = True,
    allow_hunter: bool = True,
) -> dict:
    domain = company_domain(prospect.get("website"))
    result = {
        "ticker": prospect.get("ticker"), "domain": domain,
        "added": 0, "updated": 0, "apollo_enrichments": 0,
        "public_contacts": 0, "asx_contacts": 0,
        "apollo_second_used": False, "skipped": False,
        "provider_unavailable": [], "errors": [],
    }

    snapshot_stats = sync_snapshot_contacts(conn, str(prospect["prospect_id"]))
    result["added"] += snapshot_stats["added"]
    result["updated"] += snapshot_stats["updated"]

    apollo_key = os.getenv("APOLLO_API_KEY", "").strip()
    hunter_key = os.getenv("HUNTER_API_KEY", "").strip()

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT source_provider, source_contact_id, email FROM prospect_contacts WHERE prospect_id = %s AND is_active = TRUE",
            (prospect["prospect_id"],),
        )
        existing_rows = list(cur.fetchall())
        apollo_ids = {
            str(row["source_contact_id"]) for row in existing_rows
            if "apollo" in (row["source_provider"] or "").split(",")
        }
        hunter_ids = {
            str(row["source_contact_id"]) for row in existing_rows
            if "hunter" in (row["source_provider"] or "").split(",")
        }
        hunter_ids.update(str(row["email"]) for row in existing_rows if row.get("email"))

    current_count = len(existing_rows)
    if current_count >= CONTACTS_PER_COMPANY:
        result["skipped"] = True
        _record_contact_result(conn, str(prospect["prospect_id"]), result, current_count)
        return result

    if not domain:
        result["errors"].append("Company website/domain is missing")

    if not apollo_key and not hunter_key:
        result["errors"].append("Apollo and Hunter API keys are not configured")

    if domain and hunter_key and allow_hunter:
        try:
            contacts = hunter_contacts(
                domain,
                hunter_key,
                limit=CONTACTS_PER_COMPANY - current_count,
                exclude_ids=hunter_ids,
            )
            for contact in contacts:
                _, inserted = save_contact(conn, str(prospect["prospect_id"]), contact)
                result["added" if inserted else "updated"] += 1
                if inserted:
                    current_count += 1
            if not contacts:
                result["errors"].append("hunter: no matching contact")
        except ContactProviderError as exc:
            logger.warning("hunter contact lookup failed for %s: %s", prospect.get("ticker"), exc)
            result["errors"].append(f"hunter: {exc}")
            if exc.code in {"credits_exhausted", "rate_limited"}:
                result["provider_unavailable"].append("hunter")
        except Exception as exc:
            conn.rollback()
            logger.exception("Unexpected hunter contact failure for %s", prospect.get("ticker"))
            result["errors"].append(f"hunter: {exc}")

    if domain and apollo_key and allow_apollo and current_count < CONTACTS_PER_COMPANY:
        max_apollo_calls = 2 if allow_apollo_second else 1
        for call_number in range(max_apollo_calls):
            if current_count >= CONTACTS_PER_COMPANY:
                break
            try:
                contact = apollo_contact(domain, apollo_key, apollo_ids)
                if contact:
                    result["apollo_enrichments"] += 1
                    result["apollo_second_used"] = call_number > 0
                    _, inserted = save_contact(conn, str(prospect["prospect_id"]), contact)
                    result["added" if inserted else "updated"] += 1
                    apollo_ids.add(str(contact["source_contact_id"]))
                    if inserted:
                        current_count += 1
                else:
                    result["errors"].append("apollo: no matching contact")
                    break
            except ContactProviderError as exc:
                result["errors"].append(f"apollo: {exc}")
                if exc.code in {"credits_exhausted", "rate_limited"}:
                    result["provider_unavailable"].append("apollo")
                break
            except Exception as exc:
                conn.rollback()
                logger.exception("Unexpected Apollo contact failure for %s", prospect.get("ticker"))
                result["errors"].append(f"apollo: {exc}")
                break

    if current_count < CONTACTS_PER_COMPANY:
        contacts = asx_announcement_contacts(
            str(prospect.get("ticker") or ""),
            domain_hint=domain,
            limit=CONTACTS_PER_COMPANY - current_count,
        )
        for contact in contacts:
            _, inserted = save_contact(conn, str(prospect["prospect_id"]), contact)
            result["added" if inserted else "updated"] += 1
            result["asx_contacts"] += 1
            if inserted:
                current_count += 1

    if domain and current_count < CONTACTS_PER_COMPANY:
        try:
            public_contacts = public_website_contacts(
                prospect["website"],
                limit=CONTACTS_PER_COMPANY - current_count,
            )
            for contact in public_contacts:
                _, inserted = save_contact(conn, str(prospect["prospect_id"]), contact)
                result["added" if inserted else "updated"] += 1
                result["public_contacts"] += 1
                if inserted:
                    current_count += 1
        except Exception as exc:
            logger.warning("Public website contact lookup failed for %s: %s", prospect.get("ticker"), exc)
            result["errors"].append(f"public website: {exc}")

    if current_count < CONTACTS_PER_COMPANY:
        result["errors"].append(
            f"only {current_count} of {CONTACTS_PER_COMPANY} contacts found"
        )
    _record_contact_result(conn, str(prospect["prospect_id"]), result, current_count)
    return result


def _record_contact_result(conn, prospect_id: str, result: dict, contact_count: int) -> None:
    if contact_count >= CONTACTS_PER_COMPANY:
        status = "complete"
    elif contact_count > 0:
        status = "partial"
    elif not result.get("domain"):
        status = "blocked"
    else:
        status = "not_found"
    error = "; ".join(result.get("errors") or [])[:1000] or None
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE prospect_matrix
               SET contact_enrichment_status = %s,
                   contact_enrichment_attempted_at = NOW(),
                   contact_enrichment_error = %s,
                   updated_at = NOW()
               WHERE id = %s""",
            (status, error, prospect_id),
        )
    conn.commit()
    result["contact_count"] = contact_count
    result["status"] = status
