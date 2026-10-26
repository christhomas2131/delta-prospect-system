"""
Guard rails for fetching URLs that came from external data (Phase 2).

* ``validate_public_url`` — http(s) only, hostname must resolve to a public
  address (blocks loopback, RFC1918, link-local/cloud-metadata, multicast).
* ``fetch`` — GET with per-hop redirect validation, response-size cap,
  content-type allow-list, bounded retries with jitter and Retry-After support.
* ``retry_after_seconds`` — parse a Retry-After header (seconds or HTTP date).

Used for company-website contact discovery and the announcement PDF export,
where the URL ultimately comes from a third party.
"""

from __future__ import annotations

import ipaddress
import logging
import random
import socket
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("safe_http")

MAX_BYTES_DEFAULT = 5 * 1024 * 1024
MAX_REDIRECTS = 5
HTML_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class UnsafeURL(ValueError):
    pass


class FetchError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified or (addr.version == 6 and addr.is_site_local))


def validate_public_url(url: str, *, resolver=None, allowed_hosts: tuple[str, ...] | None = None) -> str:
    """Return the URL if safe to fetch, else raise UnsafeURL."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL(f"scheme not allowed: {parsed.scheme or '(none)'}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURL("missing host")
    if parsed.username or parsed.password:
        raise UnsafeURL("credentials in URL not allowed")
    if allowed_hosts and not any(host == h or host.endswith("." + h) for h in allowed_hosts):
        raise UnsafeURL(f"host {host} not in allow-list")
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".internal"):
        raise UnsafeURL("local hostnames not allowed")
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        try:
            infos = (resolver or socket.getaddrinfo)(host, None)
            ips = sorted({info[4][0] for info in infos}) if resolver is None else list(infos)
        except socket.gaierror as exc:
            raise UnsafeURL(f"cannot resolve {host}: {exc}") from exc
    if not ips:
        raise UnsafeURL(f"{host} resolved to no addresses")
    for ip in ips:
        if not _is_public_ip(ip):
            raise UnsafeURL(f"{host} resolves to non-public address {ip}")
    return url


def retry_after_seconds(value: str | None, *, default: float = 2.0, cap: float = 60.0) -> float:
    if not value:
        return default
    value = value.strip()
    if value.isdigit():
        return min(float(value), cap)
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, min((when - datetime.now(timezone.utc)).total_seconds(), cap))
    except (TypeError, ValueError):
        return default


def fetch(url: str, *, timeout: float = 15.0, max_bytes: int = MAX_BYTES_DEFAULT,
          allowed_types: tuple[str, ...] | None = HTML_TYPES, headers: dict | None = None,
          retries: int = 2, client: httpx.Client | None = None, resolver=None) -> httpx.Response:
    """GET ``url`` safely. Follows at most MAX_REDIRECTS hops, re-validating each
    target; aborts bodies larger than ``max_bytes``; retries 429/5xx/timeouts."""
    own_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=False, headers=headers)
    try:
        attempt = 0
        while True:
            try:
                return _fetch_once(client, url, max_bytes, allowed_types, resolver)
            except FetchError as exc:
                retryable = exc.status in (429, 500, 502, 503, 504) or exc.status is None
                if not retryable or attempt >= retries:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else min(2 ** attempt, 8) + random.uniform(0, 0.5)
                logger.info("retrying %s in %.1fs (%s)", url, delay, exc)
                time.sleep(delay)
                attempt += 1
    finally:
        if own_client:
            client.close()


def _fetch_once(client: httpx.Client, url: str, max_bytes: int, allowed_types, resolver) -> httpx.Response:
    current = validate_public_url(url, resolver=resolver)
    for _hop in range(MAX_REDIRECTS + 1):
        try:
            with client.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    current = validate_public_url(httpx.URL(current).join(resp.headers["location"]).__str__(),
                                                  resolver=resolver)
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise FetchError(f"HTTP {resp.status_code}", status=resp.status_code,
                                     retry_after=retry_after_seconds(resp.headers.get("retry-after")) if resp.status_code == 429 else None)
                if resp.status_code >= 400:
                    raise FetchError(f"HTTP {resp.status_code}", status=resp.status_code)
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if allowed_types and ctype and not any(ctype.startswith(t) for t in allowed_types):
                    raise FetchError(f"content-type {ctype} not allowed", status=415)
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError(f"response too large ({declared} bytes)", status=413)
                chunks, total = [], 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError(f"response exceeded {max_bytes} bytes", status=413)
                    chunks.append(chunk)
                body = b"".join(chunks)
                return httpx.Response(resp.status_code, headers=resp.headers, content=body,
                                      request=httpx.Request("GET", current))
        except httpx.TimeoutException as exc:
            raise FetchError(f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"transport error: {exc}") from exc
    raise FetchError("too many redirects", status=310)
