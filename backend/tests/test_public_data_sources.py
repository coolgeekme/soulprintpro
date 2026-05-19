"""Tests for the public-data harvest helpers.

These cover the Feb 18, 2026 'kitchen sink' fix for lead-hunt missions
shipping 1-row CSVs in production. The fix swapped the agent's default
"scrape Chamber/BBB/Yelp" loop for direct JSON API calls to SEC EDGAR,
USAspending.gov, Wikipedia list pages, and OpenCorporates — sources
that don't return Cloudflare 403s.

We test:
  • response shape: every row carries the standard lead-hunt columns
  • state filtering on SEC EDGAR
  • US address parsing on OpenCorporates results
  • graceful empty-result handling on each helper
  • SIC code → industry mapping
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from public_data_sources import (  # noqa: E402
    search_sec_edgar,
    search_usaspending,
    search_wikipedia_companies,
    search_opencorporates,
    _sic_to_industry,
    _parse_us_address,
)


_LEAD_HUNT_COLUMNS = {
    "Business Name", "City", "State", "Industry",
    "Years in Business", "Annual Revenue (USD)",
    "Website URL", "Contact Name", "Contact Phone",
    "Contact Email", "Source URL",
}


# ── helpers ────────────────────────────────────────────────────────────────
class TestSicMapping:
    def test_known_sic_returns_industry_name(self):
        assert _sic_to_industry("7372") == "Software / SaaS"
        assert _sic_to_industry("8062") == "Hospital / Healthcare"

    def test_unknown_sic_returns_passthrough(self):
        assert _sic_to_industry("9999") == "SIC 9999"

    def test_empty_sic_returns_empty(self):
        assert _sic_to_industry("") == ""
        assert _sic_to_industry(None) == ""

    def test_list_sic_takes_first(self):
        assert _sic_to_industry(["7372", "8062"]) == "Software / SaaS"


class TestParseUSAddress:
    def test_extracts_city_and_state(self):
        c, s = _parse_us_address("123 Main St, Phoenix, AZ 85001")
        assert c == "Phoenix" and s == "AZ"

    def test_handles_address_without_zip(self):
        c, s = _parse_us_address("456 Elm Ave, Austin, TX")
        assert c == "Austin" and s == "TX"

    def test_returns_empty_when_no_state(self):
        assert _parse_us_address("Unparseable Address") == ("", "")
        assert _parse_us_address("") == ("", "")


# ── SEC EDGAR ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_sec_edgar_returns_lead_shape_rows():
    fake_response = {
        "hits": {
            "hits": [
                {"_source": {
                    "ciks": ["0001234567"],
                    "display_names": ["ACME HEALTH CORP  (0001234567) (Filer)"],
                    "biz_states": ["AZ"],
                    "sic": "8062",
                }},
                {"_source": {
                    "ciks": ["0009876543"],
                    "display_names": ["BRAVO SOFTWARE INC  (0009876543) (Filer)"],
                    "biz_states": ["AZ"],
                    "sic": "7372",
                }},
            ]
        }
    }
    with patch("public_data_sources._get_json", AsyncMock(return_value=fake_response)):
        rows = await search_sec_edgar(query="healthcare", state="AZ", limit=10)
    assert len(rows) == 2
    for r in rows:
        assert set(r.keys()) == _LEAD_HUNT_COLUMNS
        assert r["State"] == "AZ"
        assert r["Source URL"].startswith("https://www.sec.gov/")
    # Display-name cleaning strips the "(CIK) (Filer)" suffix.
    assert rows[0]["Business Name"] == "ACME HEALTH CORP"
    assert rows[0]["Industry"] == "Hospital / Healthcare"
    assert rows[1]["Industry"] == "Software / SaaS"


@pytest.mark.asyncio
async def test_sec_edgar_state_filter_drops_mismatches():
    fake_response = {
        "hits": {
            "hits": [
                {"_source": {
                    "ciks": ["0001"], "display_names": ["AZ CO  (0001) (Filer)"],
                    "biz_states": ["AZ"], "sic": "7372",
                }},
                {"_source": {
                    "ciks": ["0002"], "display_names": ["NY CO  (0002) (Filer)"],
                    "biz_states": ["NY"], "sic": "7372",
                }},
            ]
        }
    }
    with patch("public_data_sources._get_json", AsyncMock(return_value=fake_response)):
        rows = await search_sec_edgar(query="x", state="AZ", limit=10)
    assert len(rows) == 1
    assert rows[0]["State"] == "AZ"


@pytest.mark.asyncio
async def test_sec_edgar_handles_empty_response():
    with patch("public_data_sources._get_json", AsyncMock(return_value=None)):
        rows = await search_sec_edgar(query="x", limit=10)
    assert rows == []


# ── USAspending ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_usaspending_returns_lead_shape_rows():
    fake = {
        "results": [
            {"recipient_id": "abc-123", "recipient_name": "PHOENIX HEALTHCARE LLC",
             "location": {"city_name": "PHOENIX", "state_code": "AZ"}},
            {"recipient_id": "def-456", "recipient_name": "Tempe Consulting Inc",
             "location": {"city_name": "TEMPE", "state_code": "AZ"}},
        ]
    }
    # USAspending uses POST so we patch httpx.AsyncClient.post.
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: fake

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *a, **k): return mock_resp

    with patch("public_data_sources.httpx.AsyncClient", lambda **kw: FakeClient()):
        rows = await search_usaspending(state="AZ", naics_keyword="healthcare", limit=10)
    assert len(rows) == 2
    for r in rows:
        assert set(r.keys()) == _LEAD_HUNT_COLUMNS
        assert r["State"] == "AZ"
        assert r["Source URL"].startswith("https://www.usaspending.gov/recipient/")
    # Uppercase recipient names get title-cased for display.
    assert rows[0]["Business Name"] == "Phoenix Healthcare Llc"
    # City title-cased too.
    assert rows[0]["City"] == "Phoenix"


# ── Wikipedia ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_wikipedia_extracts_first_table_rows():
    search_payload = {
        "query": {
            "search": [
                {"title": "List of companies based in Phoenix, Arizona"},
                {"title": "Phoenix (band)"},
            ]
        }
    }
    article_html = """
    <html><body>
    <table class="wikitable sortable">
      <tr><th>Company</th><th>Industry</th></tr>
      <tr><td>Acme Corp</td><td>Tech</td></tr>
      <tr><td><a href="/x">Bravo Inc</a></td><td>Healthcare</td></tr>
      <tr><td>Charlie LLC</td><td>Finance</td></tr>
    </table>
    </body></html>
    """
    mock_html_resp = AsyncMock()
    mock_html_resp.status_code = 200
    mock_html_resp.text = article_html

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, *a, **k):
            return mock_html_resp

    with patch("public_data_sources._get_json", AsyncMock(return_value=search_payload)):
        with patch("public_data_sources.httpx.AsyncClient", lambda **kw: FakeClient()):
            rows = await search_wikipedia_companies(query="Phoenix, Arizona", limit=30)
    # Three data rows; the header row is skipped.
    assert len(rows) == 3
    for r in rows:
        assert set(r.keys()) == _LEAD_HUNT_COLUMNS
        assert r["City"] == "Phoenix, Arizona"
        assert "List_of_companies_based_in_Phoenix" in r["Source URL"]
    assert rows[0]["Business Name"] == "Acme Corp"
    assert rows[0]["Industry"] == "Tech"
    # HTML anchor tags get stripped from the company name.
    assert rows[1]["Business Name"] == "Bravo Inc"


@pytest.mark.asyncio
async def test_wikipedia_handles_no_article_found():
    """No matching 'List of ...' article in the search results."""
    empty_payload = {"query": {"search": []}}
    with patch("public_data_sources._get_json", AsyncMock(return_value=empty_payload)):
        rows = await search_wikipedia_companies(query="No-Such-City", limit=10)
    assert rows == []


@pytest.mark.asyncio
async def test_wikipedia_skips_irrelevant_hits():
    """Non-list pages (e.g. 'Austin Motor Company' when searching Austin)
    must NOT be treated as the article — the picker filters on 'list of
    companies/corporations' + query-token presence."""
    search_payload = {
        "query": {
            "search": [
                {"title": "Austin Motor Company"},
                {"title": "Phoenix (band)"},
            ]
        }
    }
    with patch("public_data_sources._get_json", AsyncMock(return_value=search_payload)):
        rows = await search_wikipedia_companies(query="Austin", limit=10)
    assert rows == []


# ── OpenCorporates ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_opencorporates_returns_lead_shape_rows():
    fake = {
        "results": {
            "companies": [
                {"company": {
                    "name": "ACME CONSULTING LLC",
                    "company_type": "Limited Liability Company",
                    "registered_address_in_full": "100 Main St, Phoenix, AZ 85001",
                    "opencorporates_url": "https://opencorporates.com/companies/us_az/12345",
                }},
                {"company": {
                    "name": "BRAVO LEGAL PLLC",
                    "company_type": "PLLC",
                    "registered_address_in_full": "200 Oak Ave, Scottsdale, AZ 85250",
                    "opencorporates_url": "https://opencorporates.com/companies/us_az/67890",
                }},
            ]
        }
    }
    with patch("public_data_sources._get_json", AsyncMock(return_value=fake)):
        rows = await search_opencorporates(jurisdiction="us_az", q="consulting", limit=10)
    assert len(rows) == 2
    for r in rows:
        assert set(r.keys()) == _LEAD_HUNT_COLUMNS
        assert r["State"] == "AZ"
        assert r["Source URL"].startswith("https://opencorporates.com/")
    assert rows[0]["City"] == "Phoenix"
    assert rows[1]["City"] == "Scottsdale"
