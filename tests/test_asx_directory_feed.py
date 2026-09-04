import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asx_scraper


CURRENT_CSV = '''"ASX code","Company name","GICs industry group","Listing date","Market Cap"
"BHP","BHP GROUP LIMITED","Materials","10/08/1885",123456789
"ORG","ORIGIN ENERGY LIMITED","Energy","01/01/2000",987654321
'''

LEGACY_CSV = '''ASX listed companies as at 1 January 2026
Company name,ASX code,GICS industry group
BHP GROUP LIMITED,BHP,Materials
ORIGIN ENERGY LIMITED,ORG,Energy
'''


@pytest.mark.parametrize("csv_text", [CURRENT_CSV, LEGACY_CSV])
def test_parse_asx_csv_accepts_current_and_legacy_formats(csv_text):
    listings = asx_scraper.parse_asx_csv(csv_text)

    assert [listing.ticker for listing in listings] == ["BHP", "ORG"]
    assert listings[0].company_name == "BHP GROUP LIMITED"
    assert listings[0].gics_sector == "Materials"
    assert listings[0].is_target_sector is True


def test_fetch_asx_csv_uses_legacy_fallback(monkeypatch):
    monkeypatch.setattr(asx_scraper, "ASX_CSV_URL", "https://directory.test/current")
    monkeypatch.setattr(asx_scraper, "ASX_LEGACY_CSV_URL", "https://directory.test/legacy")
    monkeypatch.setattr(asx_scraper, "ASX_CSV_FALLBACK_URLS", ("https://directory.test/legacy",))
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if request.url.path.endswith("/current"):
            return httpx.Response(404, request=request)
        return httpx.Response(200, text=LEGACY_CSV, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = asx_scraper.fetch_asx_csv(client)

    assert result == LEGACY_CSV
    assert requested == ["https://directory.test/current", "https://directory.test/legacy"]


def test_fetch_asx_csv_returns_safe_error_when_every_source_fails(monkeypatch):
    monkeypatch.setattr(asx_scraper, "ASX_CSV_URL", "https://directory.test/current")
    monkeypatch.setattr(asx_scraper, "ASX_LEGACY_CSV_URL", "https://directory.test/legacy")
    monkeypatch.setattr(asx_scraper, "ASX_CSV_FALLBACK_URLS", ("https://directory.test/legacy",))

    def handler(request):
        return httpx.Response(503, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(asx_scraper.ASXDataSourceError, match="temporarily unavailable") as exc_info:
            asx_scraper.fetch_asx_csv(client)

    assert "developer.mozilla.org" not in str(exc_info.value)


def test_validate_listing_snapshot_rejects_truncated_feed():
    listings = asx_scraper.parse_asx_csv(CURRENT_CSV)

    with pytest.raises(asx_scraper.ASXDataSourceError, match="safety minimum"):
        asx_scraper.validate_listing_snapshot(listings, minimum_count=3)


def test_validate_listing_snapshot_rejects_duplicate_tickers():
    listings = asx_scraper.parse_asx_csv(CURRENT_CSV)
    listings.append(listings[0])

    with pytest.raises(asx_scraper.ASXDataSourceError, match="duplicate company codes"):
        asx_scraper.validate_listing_snapshot(listings, minimum_count=2)
