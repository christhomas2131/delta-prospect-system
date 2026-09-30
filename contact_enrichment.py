"""Contact discovery for high-priority prospects using Apollo and Hunter."""

import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from psycopg2.extras import Json, RealDictCursor


logger = logging.getLogger("contact_enrichment")

APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
APOLLO_ENRICH_URL = "https://api.apollo.io/api/v1/people/match"
HUNTER_DOMAIN_URL = "https://api.hunter.io/v2/domain-search"

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
    pass


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


def _request_json(method: str, url: str, *, headers=None, params=None, payload=None, timeout=30):
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, url, headers=headers, params=params, json=payload)
    except httpx.HTTPError as exc:
        raise ContactProviderError(f"request failed: {exc}") from exc
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            detail = body.get("error") or body.get("message") or body.get("detail") or ""
            if isinstance(detail, dict):
                detail = detail.get("message", "")
        except Exception:
            detail = response.text[:200]
        raise ContactProviderError(f"HTTP {response.status_code}: {detail or 'provider rejected request'}")
    return response.json()


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


def hunter_contact(domain: str, api_key: str) -> dict | None:
    params = {
        "domain": domain,
        "api_key": api_key,
        "type": "personal",
        "seniority": "executive",
        "limit": 1,
    }
    result = _request_json("GET", HUNTER_DOMAIN_URL, params=params)
    emails = (result.get("data") or {}).get("emails") or []
    if not emails:
        # No-result searches do not consume Hunter credits. Broaden once.
        params.pop("seniority", None)
        result = _request_json("GET", HUNTER_DOMAIN_URL, params=params)
        emails = (result.get("data") or {}).get("emails") or []
    if not emails:
        return None
    person = emails[0]
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
                       email = COALESCE(email, %s),
                       email_status = CASE WHEN %s IS NOT NULL THEN %s ELSE email_status END,
                       phone = COALESCE(phone, %s),
                       linkedin_url = COALESCE(linkedin_url, %s),
                       confidence_score = GREATEST(COALESCE(confidence_score, 0), %s),
                       last_verified_at = %s,
                       updated_at = %s
                   WHERE id = %s""",
                (
                    ",".join(sorted(providers)), email, email, contact.get("email_status"),
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
) -> dict:
    domain = company_domain(prospect.get("website"))
    result = {
        "ticker": prospect.get("ticker"), "domain": domain,
        "added": 0, "updated": 0, "apollo_enrichments": 0,
        "apollo_second_used": False, "errors": [],
    }
    if not domain:
        result["errors"].append("Company website/domain is missing")
        return result

    apollo_key = os.getenv("APOLLO_API_KEY", "").strip()
    hunter_key = os.getenv("HUNTER_API_KEY", "").strip()
    if not apollo_key and not hunter_key:
        result["errors"].append("Apollo and Hunter API keys are not configured")
        return result

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT source_provider, source_contact_id FROM prospect_contacts WHERE prospect_id = %s AND is_active = TRUE",
            (prospect["prospect_id"],),
        )
        existing_rows = list(cur.fetchall())
        existing_sources = {source for row in existing_rows for source in (row["source_provider"] or "").split(",")}
        apollo_ids = {
            str(row["source_contact_id"]) for row in existing_rows
            if "apollo" in (row["source_provider"] or "").split(",")
        }

    providers = []
    if apollo_key and (force or "apollo" not in existing_sources):
        providers.append(("apollo", lambda: apollo_contact(domain, apollo_key, apollo_ids)))
    if hunter_key and (force or "hunter" not in existing_sources):
        providers.append(("hunter", lambda: hunter_contact(domain, hunter_key)))

    for provider, fetch_contact in providers:
        try:
            contact = fetch_contact()
            if provider == "apollo" and contact:
                result["apollo_enrichments"] += 1
            if not contact:
                result["errors"].append(f"{provider}: no matching contact")
                continue
            _, inserted = save_contact(conn, str(prospect["prospect_id"]), contact)
            result["added" if inserted else "updated"] += 1
        except ContactProviderError as exc:
            logger.warning("%s contact lookup failed for %s: %s", provider, prospect.get("ticker"), exc)
            result["errors"].append(f"{provider}: {exc}")
        except Exception as exc:
            conn.rollback()
            logger.exception("Unexpected %s contact failure for %s", provider, prospect.get("ticker"))
            result["errors"].append(f"{provider}: {exc}")

    if allow_apollo_second and apollo_key:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT source_provider, source_contact_id FROM prospect_contacts WHERE prospect_id = %s AND is_active = TRUE",
                (prospect["prospect_id"],),
            )
            current_rows = list(cur.fetchall())
        if len(current_rows) < 2:
            used_ids = {
                str(row["source_contact_id"]) for row in current_rows
                if "apollo" in (row["source_provider"] or "").split(",")
            }
            try:
                contact = apollo_contact(domain, apollo_key, used_ids)
                if contact:
                    result["apollo_enrichments"] += 1
                    result["apollo_second_used"] = True
                    _, inserted = save_contact(conn, str(prospect["prospect_id"]), contact)
                    result["added" if inserted else "updated"] += 1
                else:
                    result["errors"].append("apollo: no second matching contact")
            except ContactProviderError as exc:
                result["errors"].append(f"apollo second contact: {exc}")
            except Exception as exc:
                conn.rollback()
                logger.exception("Unexpected second Apollo contact failure for %s", prospect.get("ticker"))
                result["errors"].append(f"apollo second contact: {exc}")
    return result
