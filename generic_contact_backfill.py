"""One-off Hunter fallback that saves public generic company inboxes."""

import os
import time

import httpx
from psycopg2.extras import RealDictCursor

from contact_enrichment import company_domain, save_contact


HUNTER_DOMAIN_URL = "https://api.hunter.io/v2/domain-search"


def run(conn) -> dict:
    api_key = os.getenv("HUNTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("HUNTER_API_KEY is not configured")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT pm.id AS prospect_id, l.ticker, l.website
            FROM prospect_matrix pm
            JOIN asx_listings l ON l.id = pm.listing_id
            LEFT JOIN prospect_contacts pc ON pc.prospect_id = pm.id AND pc.is_active = TRUE
            WHERE l.is_active = TRUE
              AND l.is_target_sector = TRUE
              AND pm.status NOT IN ('disqualified', 'archived')
              AND l.website IS NOT NULL AND BTRIM(l.website) <> ''
            GROUP BY pm.id, l.id
            HAVING COUNT(pc.id) = 0
            ORDER BY pm.prospect_score DESC NULLS LAST, l.ticker
        """)
        prospects = list(cur.fetchall())

    stats = {"total": len(prospects), "processed": 0, "added": 0, "updated": 0, "companies_found": 0, "failures": 0, "provider_limit": False}
    print(f"START total={stats['total']}", flush=True)
    with httpx.Client(timeout=30) as client:
        for index, prospect in enumerate(prospects, start=1):
            domain = company_domain(prospect["website"])
            try:
                response = client.get(HUNTER_DOMAIN_URL, params={
                    "domain": domain, "api_key": api_key, "type": "generic", "limit": 2,
                })
                if response.status_code == 429:
                    stats["provider_limit"] = True
                    print(f"STOP provider_limit ticker={prospect['ticker']}", flush=True)
                    break
                response.raise_for_status()
                emails = ((response.json().get("data") or {}).get("emails") or [])
                saved_here = 0
                for person in emails:
                    email = (person.get("value") or "").strip().lower()
                    if not email:
                        continue
                    verification = person.get("verification") or {}
                    contact = {
                        "source_provider": "hunter_generic",
                        "source_contact_id": email,
                        "full_name": person.get("first_name") or person.get("last_name") or "General contact",
                        "first_name": person.get("first_name"),
                        "last_name": person.get("last_name"),
                        "title": person.get("position") or "General contact",
                        "seniority": person.get("seniority"),
                        "departments": [person["department"]] if person.get("department") else [],
                        "email": email,
                        "email_status": verification.get("status") or "available",
                        "phone": person.get("phone_number"),
                        "linkedin_url": person.get("linkedin"),
                        "confidence_score": float(person["confidence"]) / 100 if person.get("confidence") is not None else 0.5,
                        "raw_data": person,
                    }
                    _, inserted = save_contact(conn, str(prospect["prospect_id"]), contact)
                    stats["added" if inserted else "updated"] += 1
                    saved_here += 1
                stats["companies_found"] += bool(saved_here)
            except Exception as exc:
                stats["failures"] += 1
                print(f"ERROR ticker={prospect['ticker']} detail={str(exc)[:140]}", flush=True)
            stats["processed"] = index
            if index == 1 or index % 10 == 0:
                print(f"PROGRESS {index}/{stats['total']} added={stats['added']} companies_found={stats['companies_found']} failures={stats['failures']}", flush=True)
            time.sleep(0.15)
    print(f"COMPLETE {stats}", flush=True)
    return stats
