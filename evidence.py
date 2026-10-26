"""
Stable evidence identity for AI-generated findings (Phase 2).

Before this module, AI findings were stored with
``claude-deep://{ticker}/{pillar}/{hash(summary + time.time())}`` — a key that
changed on every run, so re-analysing a company duplicated the same finding
and inflated its score. Keys are now deterministic:

* ``evidence_key`` = sha1(ticker | pillar | normalised claim) — identifies the
  underlying finding regardless of wording tweaks or which run produced it.
* ``source_url``   = the real document/announcement URL when the model cited one
  we supplied; otherwise ``claude-deep://{ticker}/{pillar}/{evidence_key[:16]}``.

Re-analysis therefore hits the existing (prospect, pillar, source_url) row and
is recorded as a revision (revision_count, last_seen_at), not a new signal.
"""

import hashlib
import re

_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "with", "its", "is", "are", "was", "were",
    "has", "have", "had", "be", "been", "by", "at", "as", "that", "this", "from", "company", "companys",
}
_NUMBER = re.compile(r"\d[\d,.%]*")


def normalise_claim(text: str | None) -> str:
    """Lower-case, strip numbers/punctuation/stopwords, sort tokens — so
    'Production guidance cut by 12% at Mine X' and 'Mine X production guidance cut'
    collapse to the same claim identity."""
    text = _NUMBER.sub(" ", (text or "").lower())
    tokens = [t for t in re.split(r"[^a-z]+", text) if len(t) > 2 and t not in _STOPWORDS]
    stems = sorted({t[:6] for t in tokens})  # crude stemming: first 6 chars
    return " ".join(stems)


def evidence_key(ticker: str, pillar: str, claim: str | None) -> str:
    material = f"{(ticker or '').upper()}|{(pillar or '').lower()}|{normalise_claim(claim)}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def ai_source_url(ticker: str, pillar: str, key: str, cited_url: str | None = None) -> str:
    if cited_url and cited_url.startswith(("http://", "https://")):
        return cited_url
    return f"claude-deep://{(ticker or '').upper()}/{(pillar or '').lower()}/{key[:16]}"


def legacy_duplicate_groups(rows: list[dict]) -> list[list[dict]]:
    """Group legacy AI signals that describe the same finding.
    rows need: id, prospect_id, pressure_type, summary, ticker. Returns groups of size > 1."""
    buckets: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (str(row["prospect_id"]), row["pressure_type"],
               evidence_key(row.get("ticker", ""), row["pressure_type"], row.get("summary")))
        buckets.setdefault(key, []).append(row)
    return [group for group in buckets.values() if len(group) > 1]


def choose_keeper(group: list[dict]) -> dict:
    """Keep the best-supported row: analyst/AI-confirmed first, then higher
    confidence, then the earliest detection (longest history)."""
    rank = {"analyst_confirmed": 0, "ai_confirmed": 1, "ai_generated": 2, "rule_matched": 2, "unverified": 3}
    return sorted(
        group,
        key=lambda r: (
            rank.get(r.get("verification_status") or "", 2),
            -(float(r.get("confidence_score") or 0)),
            r.get("detected_at") or "",
        ),
    )[0]
