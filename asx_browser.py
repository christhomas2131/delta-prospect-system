"""
asx_browser.py — ASX Announcements Fetcher (Markit Digital API)
================================================================
Fetches ASX announcement headlines via the Markit Digital API that
powers the official ASX website. Uses a static Bearer token embedded
in the ASX page JavaScript. No browser automation required.

If the token ever expires (401/403), a Playwright fallback automatically
visits the ASX company page with real Chrome to harvest a fresh token,
then retries.

Returns the same announcement list format that enrichment_agent.py expects:
    [{"title": str, "url": str, "date": str}, ...]

Usage (as library):
    fetcher = ASXFetcher()
    anns = fetcher.fetch_announcements("BHP")
    anns = fetcher.fetch_announcements("RIO")

Usage (standalone test):
    python asx_browser.py BHP
    python asx_browser.py BHP RIO FMG MIN
"""

import json, logging, sys, time
import httpx

logger = logging.getLogger("asx_browser")

# ---------------------------------------------------------------------------
# Markit Digital API — the backend that powers asx.com.au company pages
# ---------------------------------------------------------------------------

MARKIT_URL   = "https://asx.api.markitdigital.com/asx-research/1.0/companies/{ticker}/announcements"
MARKIT_TOKEN = "83ff96335c2d45a094df02a206a39ff4"   # static token embedded in ASX page JS

MARKIT_HEADERS = {
    "Authorization": f"Bearer {MARKIT_TOKEN}",
    "Accept":        "application/json, text/plain, */*",
    "Accept-Language": "en-AU",
    "Referer":       "https://www.asx.com.au/",
    "User-Agent":    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# ASX document URL pattern — constructed from documentKey in the response
ASX_DOC_URL  = "https://www.asx.com.au/asx/1/file/{key}/announcements"

HTTP_TIMEOUT = 15
BETWEEN_REQUESTS = 2.0


def _parse_markit_response(data: dict) -> list[dict]:
    """Extract announcements from the Markit API response structure."""
    items = data.get("data", {}).get("items") or []
    results = []
    for item in items:
        title = item.get("headline") or item.get("header") or item.get("title") or ""
        if not title:
            continue
        date_str = (item.get("date") or item.get("announcement_date") or "")
        if date_str:
            date_str = str(date_str)[:10]   # trim to YYYY-MM-DD
        # Build a stable URL from the documentKey so DB constraints work correctly
        doc_key = item.get("documentKey") or ""
        url_val = ASX_DOC_URL.format(key=doc_key) if doc_key else (item.get("url") or "")
        results.append({"title": title, "url": url_val, "date": date_str})
    return results


# ---------------------------------------------------------------------------
# Main fetcher class
# ---------------------------------------------------------------------------

class ASXFetcher:
    """
    Fetches ASX announcements via the Markit Digital API.
    Reuses a single httpx.Client across calls for connection pooling.
    Falls back to Playwright to refresh the Bearer token if it expires.
    """

    def __init__(self):
        self._client = httpx.Client(
            headers=MARKIT_HEADERS,
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
        )
        self._token = MARKIT_TOKEN
        self._playwright_refresh_attempted = False

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_announcements(self, ticker: str) -> list[dict]:
        """
        Fetch recent announcements for a ticker.
        Returns [] on failure.
        """
        ticker = ticker.upper()
        url = MARKIT_URL.format(ticker=ticker)
        try:
            resp = self._client.get(url)
        except Exception as exc:
            logger.error("%s: HTTP error: %s", ticker, exc)
            return []

        if resp.status_code in (401, 403):
            logger.warning("%s: token rejected (HTTP %s) — attempting token refresh", ticker, resp.status_code)
            if self._refresh_token():
                return self.fetch_announcements(ticker)   # one retry
            return []

        if resp.status_code != 200:
            logger.warning("%s: HTTP %s", ticker, resp.status_code)
            return []

        try:
            data = resp.json()
        except Exception:
            logger.error("%s: JSON parse failed", ticker)
            return []

        results = _parse_markit_response(data)
        logger.info("%s: %d announcements", ticker, len(results))
        return results

    def fetch_batch(self, tickers: list[str], delay: float = BETWEEN_REQUESTS) -> dict:
        """Fetch announcements for multiple tickers. Returns {TICKER: [items]}."""
        results = {}
        for i, ticker in enumerate(tickers):
            results[ticker.upper()] = self.fetch_announcements(ticker)
            if i < len(tickers) - 1:
                time.sleep(delay)
        return results

    # ------------------------------------------------------------------
    # Token refresh via Playwright (fallback)
    # ------------------------------------------------------------------

    def _refresh_token(self) -> bool:
        """
        Visit the ASX BHP page with Playwright and steal the fresh Bearer
        token from the announcements XHR request headers.
        Returns True if a new token was obtained and applied.
        """
        if self._playwright_refresh_attempted:
            logger.error("Token refresh already attempted — giving up")
            return False
        self._playwright_refresh_attempted = True

        logger.info("Launching Playwright to refresh Bearer token...")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("Playwright not installed — cannot refresh token")
            return False

        new_token = None
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(
                    user_agent=MARKIT_HEADERS["User-Agent"],
                    viewport={"width": 1280, "height": 800},
                    locale="en-AU",
                )
                page = ctx.new_page()

                def on_request(req):
                    nonlocal new_token
                    if "markitdigital" in req.url and "/announcements" in req.url:
                        auth = req.headers.get("authorization", "")
                        if auth.startswith("Bearer "):
                            new_token = auth.split(" ", 1)[1]
                            logger.info("Captured fresh token from browser request")

                page.on("request", on_request)
                page.goto("https://www.asx.com.au/markets/company/BHP",
                          wait_until="domcontentloaded", timeout=30000)
                # Scroll to trigger announcements widget
                for pos in [500, 1000, 1500, 2000]:
                    page.evaluate(f"window.scrollTo(0, {pos})")
                    time.sleep(0.5)
                time.sleep(8)
                browser.close()
        except Exception as exc:
            logger.error("Playwright token refresh failed: %s", exc)
            return False

        if new_token and new_token != self._token:
            self._token = new_token
            self._client.headers["Authorization"] = f"Bearer {new_token}"
            logger.info("Token refreshed successfully")
            return True

        logger.warning("Could not capture a new token from Playwright")
        return False


# ---------------------------------------------------------------------------
# Backward-compatible alias (used by enrichment_agent.py)
# ---------------------------------------------------------------------------

# Allow import as: from asx_browser import ASXBrowser
ASXBrowser = ASXFetcher


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["BHP"]
    print(f"\nTesting ASXFetcher with: {tickers}\n")

    with ASXFetcher() as fetcher:
        for i, tk in enumerate(tickers):
            anns = fetcher.fetch_announcements(tk)
            print(f"\n{'='*60}")
            print(f"  {tk}: {len(anns)} announcements")
            print(f"{'='*60}")
            for a in anns:
                print(f"  [{a['date']}] {a['title'][:80]}")
            if i < len(tickers) - 1:
                time.sleep(BETWEEN_REQUESTS)
