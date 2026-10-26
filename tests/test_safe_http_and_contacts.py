"""Phase 2: URL guard rails, bounded fetch, provider retry policy, contact provenance labels."""

import httpx
import pytest

import contact_enrichment
import safe_http
from contact_enrichment import classify_contact
from safe_http import FetchError, UnsafeURL, fetch, retry_after_seconds, validate_public_url


def public(host, *_):
    return ["93.184.216.34"]  # a real, globally routable address (TEST-NET ranges count as reserved)


@pytest.mark.parametrize("url", [
    "ftp://example.com/x", "file:///etc/passwd", "http://localhost/admin", "http://127.0.0.1:8000/",
    "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/", "http://192.168.1.1/", "http://[::1]/",
    "http://user:pw@example.com/", "http://metadata.internal/",
])
def test_rejects_private_or_unsafe_urls(url):
    with pytest.raises(UnsafeURL):
        validate_public_url(url, resolver=public)


def test_rejects_hostname_resolving_to_private_ip():
    with pytest.raises(UnsafeURL, match="non-public"):
        validate_public_url("https://intranet.example.com/", resolver=lambda h, *_: ["10.1.2.3"])


def test_accepts_public_and_allowlisted():
    assert validate_public_url("https://www.asx.com.au/x", resolver=public, allowed_hosts=("asx.com.au",))
    with pytest.raises(UnsafeURL, match="allow-list"):
        validate_public_url("https://evil.com/x", resolver=public, allowed_hosts=("asx.com.au",))


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_redirect_to_private_address_is_blocked():
    def handler(request):
        if request.url.host == "start.example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/"})
        return httpx.Response(200, text="should not get here")
    with pytest.raises(UnsafeURL):
        fetch("https://start.example.com/", client=_client(handler), resolver=public, retries=0)


def test_oversized_and_wrong_content_type_are_rejected():
    big = httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 2048)
    with pytest.raises(FetchError, match="too large|exceeded"):
        fetch("https://a.example.com/", client=_client(lambda r: big), resolver=public, max_bytes=1024, retries=0)
    pdf = httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
    with pytest.raises(FetchError, match="content-type"):
        fetch("https://a.example.com/", client=_client(lambda r: pdf), resolver=public, retries=0)


def test_429_honours_retry_after_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps = []
    monkeypatch.setattr(safe_http.time, "sleep", lambda s: sleeps.append(s))

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="ok")
    resp = fetch("https://a.example.com/", client=_client(handler), resolver=public, retries=2)
    assert resp.text == "ok" and calls["n"] == 2 and sleeps == [7.0]


def test_retry_after_parsing():
    assert retry_after_seconds("5") == 5.0
    assert retry_after_seconds("9999", cap=60) == 60.0
    assert retry_after_seconds(None, default=2.0) == 2.0
    assert retry_after_seconds("garbage", default=1.5) == 1.5


def test_provider_request_retries_transient_then_surfaces_rate_limit(monkeypatch):
    attempts = {"n": 0}
    monkeypatch.setattr(contact_enrichment, "httpx", httpx)
    sleeps = []
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: sleeps.append(s))

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, *a, **k):
            attempts["n"] += 1
            return httpx.Response(429, headers={"retry-after": "1"}, json={"error": "too many"})
    monkeypatch.setattr(httpx, "Client", FakeClient)
    with pytest.raises(contact_enrichment.ContactProviderError) as exc:
        contact_enrichment._request_json("GET", "https://api.hunter.io/x", retries=2)
    assert exc.value.code == "rate_limited" and attempts["n"] == 3


def test_contact_classification_never_promotes_generic_inbox():
    assert classify_contact({"email": "investors@acme.com.au", "full_name": "Investor Relations", "title": "Investor Relations",
                             "source_provider": "public_website", "email_status": "unverified"}) == "generic_inbox"
    assert classify_contact({"email": "info@acme.com.au", "full_name": "Jane Doe", "source_provider": "hunter", "email_status": "valid"}) == "generic_inbox"
    assert classify_contact({"email": "jane.doe@acme.com.au", "full_name": "Jane Doe", "source_provider": "asx_announcement",
                             "email_status": "unverified", "raw_data": {"inferred_from_email": True}}) == "named_inferred"
    assert classify_contact({"email": "jane.doe@acme.com.au", "full_name": "Jane Doe", "source_provider": "hunter", "email_status": "valid"}) == "named_verified"
    assert classify_contact({"email": "jane.doe@acme.com.au", "full_name": "Jane Doe", "source_provider": "snapshot", "email_status": "available"}) == "named_verified"
    assert classify_contact({"email": None, "full_name": "Jane Doe", "source_provider": "apollo", "email_status": None}) == "named_unverified"
    assert classify_contact({"email": "j.doe@acme.com.au", "full_name": "Jane Doe", "source_provider": "public_website", "email_status": "unverified"}) == "named_unverified"
