"""One-off public website fallback for companies with no saved contacts."""

import html
import os
import re
import time
from urllib.parse import urljoin, urlparse

import httpx
from psycopg2.extras import RealDictCursor

from contact_enrichment import save_contact


EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+(?![\w.-])")
CONTACT_HINTS = ("contact", "about", "team", "investor", "corporate", "support")
SKIP_DOMAINS = {"example.com", "sentry.io", "wixpress.com", "wordpress.org"}


def _emails(page: str) -> list[str]:
    found = []
    for email in EMAIL_RE.findall(html.unescape(page)):
        email = email.lower().strip(".,;:!?)]}\"'")
        domain = email.rsplit("@", 1)[-1]
        if domain not in SKIP_DOMAINS and email not in found:
            found.append(email)
    return found


def _candidate_pages(base_url: str, homepage: str) -> list[str]:
    candidates = [base_url]
    for href in re.findall(r'''href=["']([^"'#?]+)["']''', homepage, flags=re.I):
        absolute = urljoin(base_url, html.unescape(href))
        parsed = urlparse(absolute)
        if parsed.netloc != urlparse(base_url).netloc:
            continue
        if any(hint in parsed.path.lower() for hint in CONTACT_HINTS) and absolute not in candidates:
            candidates.append(absolute)
    return candidates[:6]


def run(conn) -> dict:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT pm.id AS prospect_id, l.ticker, l.website
            FROM prospect_matrix pm
            JOIN asx_listings l ON l.id = pm.listing_id
            LEFT JOIN prospect_contacts pc ON pc.prospect_id = pm.id AND pc.is_active = TRUE
            WHERE l.is_active = TRUE AND l.is_target_sector = TRUE
              AND pm.status NOT IN ('disqualified', 'archived')
              AND l.website IS NOT NULL AND BTRIM(l.website) <> ''
            GROUP BY pm.id, l.id
            HAVING COUNT(pc.id) = 0
            ORDER BY pm.prospect_score DESC NULLS LAST, l.ticker
        """)
        prospects = list(cur.fetchall())

    stats = {"total": len(prospects), "processed": 0, "added": 0, "companies_found": 0, "failures": 0}
    print(f"START total={stats['total']}", flush=True)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DeltaProspectContactCheck/1.0)"}
    with httpx.Client(timeout=15, follow_redirects=True, headers=headers) as client:
        for index, prospect in enumerate(prospects, start=1):
            base_url = prospect["website"].strip()
            if not base_url.startswith(("http://", "https://")):
                base_url = "https://" + base_url
            try:
                homepage = client.get(base_url).text
                emails = _emails(homepage)
                for url in _candidate_pages(base_url, homepage)[1:]:
                    try:
                        emails.extend(_emails(client.get(url).text))
                    except httpx.HTTPError:
                        continue
                emails = list(dict.fromkeys(emails))[:2]
                saved = 0
                for email in emails:
                    _, inserted = save_contact(conn, str(prospect["prospect_id"]), {
                        "source_provider": "public_website",
                        "source_contact_id": email,
                        "full_name": "General contact",
                        "title": "General contact",
                        "departments": [],
                        "email": email,
                        "email_status": "unverified",
                        "confidence_score": 0.45,
                        "raw_data": {"website": base_url, "email": email},
                    })
                    stats["added"] += int(inserted)
                    saved += int(inserted)
                stats["companies_found"] += bool(saved)
            except httpx.HTTPError:
                stats["failures"] += 1
            stats["processed"] = index
            if index == 1 or index % 10 == 0:
                print(f"PROGRESS {index}/{stats['total']} added={stats['added']} companies_found={stats['companies_found']} failures={stats['failures']}", flush=True)
            time.sleep(0.25)
    print(f"COMPLETE {stats}", flush=True)
    return stats
