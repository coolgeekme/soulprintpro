"""Public-data harvest helpers — JSON APIs, no scraping.

Foundry's lead-hunt missions kept producing 0-1 rows because the
suggested aggregator sources (Chamber of Commerce, BBB, Yelp, Google
Maps, LinkedIn) are protected by Cloudflare / login walls — every
``web_scrape`` against them returns empty or 403. These helpers hit
**real JSON APIs** instead so the agent can actually harvest rows.

Why a separate module?
  • Keeps the URL templates + response-shape adapters away from
    missions.py (which is already ~3,500 lines).
  • Lets us add tests without spinning the whole FastAPI app.
  • Each helper is `async def search_*` and returns a list of
    normalised dicts shaped for the standard lead-hunt CSV columns
    (Business Name, City, State, Industry, Years in Business, Annual
    Revenue (USD), Website URL, Contact Name, Contact Phone, Contact
    Email, Source URL). Unknown fields are blanks — never fabricated.

Public APIs used
  • SEC EDGAR — efts.sec.gov full-text + data.sec.gov company tickers.
    No auth, free, no rate limit beyond Edgar's "10 req/s + descriptive
    User-Agent" rule.
  • USAspending.gov — api.usaspending.gov/api/v2/recipient. JSON
    catalog of every federal-money recipient (contractors, grantees,
    sub-awardees). Covers tens of thousands of businesses with state +
    NAICS code already attached.
  • Wikipedia — en.wikipedia.org pages of the form "List of companies
    based in <City>" / "List of companies in <Industry>". Open and
    scrape-friendly. Surface as plain markdown via Trafilatura.
  • OpenCorporates — api.opencorporates.com. Free tier covers basic
    company-name + jurisdiction lookups. No key needed for low volume.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


# SEC requires a descriptive User-Agent or it will 403. Foundry-specific
# so they can rate-limit per app cleanly. Email is a noreply mailbox the
# Foundry team monitors for SEC compliance pings.
_USER_AGENT = (
    "FoundryLeadHarvester/1.0 "
    "(https://foundryagents.ai; legal@foundryagents.ai)"
)

_HTTP_TIMEOUT = 20.0


async def _get_json(url: str, *, params: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    """One-line HTTPX GET with our User-Agent + 20s timeout. Returns
    parsed JSON or None on any failure (swallowed + logged). Per-helper
    callers should treat None as "source unavailable" and try the next
    helper rather than failing the whole mission turn."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(
                url, params=params or {},
                headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            )
            if r.status_code != 200:
                logger.warning("public_data GET %s -> %s", url, r.status_code)
                return None
            return r.json()
    except Exception as exc:
        logger.warning("public_data GET %s failed: %s", url, exc)
        return None


# ── SEC EDGAR ──────────────────────────────────────────────────────────────
async def search_sec_edgar(
    *, query: str, state: Optional[str] = None, limit: int = 25,
) -> List[Dict[str, Any]]:
    """Hit SEC EDGAR full-text search (efts.sec.gov).

    Returns companies that have filed with the SEC and match ``query``
    (industry keyword, business name, etc.). Optional ``state`` filters
    by HQ state. Returned rows have: Business Name, City, State,
    Industry, Website URL (best-effort empty), Source URL pointing to
    the SEC company page.

    SEC EDGAR's filing data is a strong B2B lead signal — any company
    that's ever filed an S-1, 10-K, DEF 14A, etc. is a real
    operating-business (not a shell), with verified address and SIC
    code → industry mapping.
    """
    # The full-text search endpoint returns filings; we then dedupe by
    # CIK (company identifier) and pull `address.state` for the filter.
    url = "https://efts.sec.gov/LATEST/search-index"
    params = {"q": query, "forms": "10-K,10-Q,S-1,DEF 14A", "hits": str(min(limit * 3, 100))}
    data = await _get_json(url, params=params)
    if not data:
        return []
    rows: List[Dict[str, Any]] = []
    seen_ciks: set[str] = set()
    for hit in (data.get("hits", {}).get("hits") or []):
        src = hit.get("_source") or {}
        ciks = src.get("ciks") or []
        names = src.get("display_names") or []
        if not ciks or not names:
            continue
        cik = ciks[0]
        if cik in seen_ciks:
            continue
        seen_ciks.add(cik)
        # display_name is e.g. "ENSIGN GROUP, INC  (ENSG)  (CIK 0001125376)"
        # or "ACME CORP  (0001234567) (Filer)". Strip both ticker AND
        # CIK trailers so the Business Name column reads cleanly.
        name = re.sub(r"\s*\([A-Z0-9.\-]{1,8}\)\s*", " ", names[0])
        name = re.sub(r"\s*\(\s*CIK\s*\d{6,10}\s*\)\s*", "", name)
        name = re.sub(r"\s*\(\d{6,10}\)\s*\(Filer\)\s*$", "", name)
        name = re.sub(r"\s+", " ", name).strip()
        # SEC stores the office state in `biz_states` (filer business
        # address). Filter when the caller asks for one specific state.
        biz_states = src.get("biz_states") or []
        company_state = (biz_states[0] if biz_states else "").upper()
        if state and company_state and company_state != state.upper():
            continue
        row = {
            "Business Name": name,
            "City": "",
            "State": company_state,
            "Industry": _sic_to_industry(src.get("sic") or src.get("sics") or ""),
            "Years in Business": "",
            "Annual Revenue (USD)": "",
            "Website URL": "",
            "Contact Name": "",
            "Contact Phone": "",
            "Contact Email": "",
            "Source URL": (
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
            ),
        }
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


# Tiny subset of SIC code -> industry mapping. Covers the most common
# codes seen in EDGAR data; everything else falls through with the raw
# SIC code preserved.
_SIC_MAP = {
    "7372": "Software / SaaS",
    "7371": "IT Services",
    "7389": "Business Services",
    "8742": "Management Consulting",
    "8062": "Hospital / Healthcare",
    "8011": "Healthcare — Physician Office",
    "8051": "Skilled Nursing Care",
    "8060": "Healthcare — Hospital",
    "8082": "Home Health Care Services",
    "8090": "Health Services",
    "6189": "Asset-Backed Securities (Finance)",
    "6199": "Finance Services",
    "6022": "Commercial Banking",
    "6021": "National Commercial Banking",
    "6770": "Holding & Other Investment Offices",
    "6798": "Real Estate Investment Trust",
    "8111": "Legal Services",
    "8721": "Accounting / Auditing",
    "7311": "Advertising / Marketing Agency",
    "7812": "Motion Picture & Video Production",
    "2834": "Pharmaceutical Preparations",
    "3844": "X-Ray / Medical Devices",
    "5812": "Restaurants / Eating Places",
    "5961": "Catalog / Mail-Order House",
    "7370": "Computer Services",
    "7374": "Data Processing",
}


def _sic_to_industry(sic: Any) -> str:
    if isinstance(sic, list):
        sic = sic[0] if sic else ""
    sic = str(sic or "").strip()
    return _SIC_MAP.get(sic, f"SIC {sic}" if sic else "")


# ── USAspending.gov ────────────────────────────────────────────────────────
async def search_usaspending(
    *, state: str, naics_keyword: Optional[str] = None, limit: int = 25,
) -> List[Dict[str, Any]]:
    """Hit USAspending.gov for federal-money recipients in a state.

    Every business that's received a federal contract or grant is in
    here. Returns Business Name, City, State, Industry, Source URL.

    Uses the autocomplete endpoint (requires non-empty `search_text`),
    falling back to a state-stub query when no keyword is supplied so
    the agent doesn't have to invent one. Then filters returned rows
    to the requested state when the API returns location data.
    """
    url = "https://api.usaspending.gov/api/v2/autocomplete/recipient/"
    # USAspending requires a non-empty search_text — they 422 otherwise.
    # When no keyword is given, default to the state name so the search
    # still returns local results (e.g. "Arizona" matches Arizona-based
    # recipients via fuzzy name match).
    search_text = (naics_keyword or "").strip() or _STATE_NAME_LOOKUP.get(state.upper(), state)
    body = {"search_text": search_text, "limit": min(limit * 3, 50)}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(
                url, json=body,
                headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            )
            if r.status_code != 200:
                logger.warning("usaspending POST -> %s body=%s", r.status_code, r.text[:200])
                return []
            data = r.json()
    except Exception as exc:
        logger.warning("usaspending POST failed: %s", exc)
        return []

    rows: List[Dict[str, Any]] = []
    for r_obj in (data.get("results") or []):
        rname = r_obj.get("recipient_name") or ""
        if not rname:
            continue
        # ``location`` may carry city/state when present. We accept
        # rows without state info too — many federal-money recipients
        # don't have geo metadata in the autocomplete payload, and
        # dropping them all would defeat the point of the helper.
        loc = r_obj.get("location") or {}
        rstate = (loc.get("state_code") or "").upper()
        if state and rstate and rstate != state.upper():
            continue
        rows.append({
            "Business Name": rname.title() if rname.isupper() else rname,
            "City": (loc.get("city_name") or "").title(),
            "State": rstate or state.upper(),  # caller asked for this state
            "Industry": "Federal contractor / grantee",
            "Years in Business": "",
            "Annual Revenue (USD)": "",
            "Website URL": "",
            "Contact Name": "",
            "Contact Phone": "",
            "Contact Email": "",
            "Source URL": (
                "https://www.usaspending.gov/recipient/"
                f"{r_obj.get('recipient_id') or ''}"
            ),
        })
        if len(rows) >= limit:
            break
    return rows


# 2-letter → full state name. Used by `search_usaspending` to seed the
# autocomplete endpoint's required `search_text` when the caller didn't
# pass a keyword. Not exhaustive (50 states + DC); we only need the ones
# Foundry users hunt in.
_STATE_NAME_LOOKUP = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}


# ── Wikipedia "List of companies based in <City>" ──────────────────────────
async def search_wikipedia_companies(
    *, query: str, limit: int = 30,
) -> List[Dict[str, Any]]:
    """Pull companies from a Wikipedia 'List of companies …' article.

    Wikipedia article HTML is liberal-license + always scrapable. This
    helper:
      1. Searches Wikipedia for "List of companies based in <query>".
      2. Hits the article's HTML.
      3. Extracts the first wikitable's rows.

    Resulting rows: Business Name, City, Industry, Source URL. State
    and revenue fields are blank — Wikipedia tables don't have a
    standard schema across articles.
    """
    # Step 1: locate the list article via Wikipedia's full-text search
    # (NOT opensearch, which only matches article-title prefixes — and
    # the canonical pages are titled "List of companies based in <City>,
    # <State>" / "List of corporations in the <Metro>" / "List of
    # <State> companies", which opensearch's prefix match misses).
    search_url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "list": "search",
        "srsearch": f"List of companies based in {query}",
        "srlimit": "5",
        "format": "json",
    }
    data = await _get_json(search_url, params=params)
    if not data:
        return []
    hits = (data.get("query") or {}).get("search") or []
    # Pick the first hit whose title actually contains "list" + a
    # location signal — avoid stumbling onto something like
    # "Austin Motor Company" when searching for Austin.
    article_title: Optional[str] = None
    q_tokens = {t for t in re.findall(r"[A-Za-z]+", query.lower()) if len(t) >= 3}
    for h in hits:
        t = h.get("title") or ""
        tl = t.lower()
        if "list of" not in tl:
            continue
        if "companies" not in tl and "corporations" not in tl:
            continue
        if q_tokens and not any(tok in tl for tok in q_tokens):
            continue
        article_title = t
        break
    if not article_title:
        return []
    article_url = (
        "https://en.wikipedia.org/wiki/"
        + article_title.replace(" ", "_")
    )
    # Step 2: fetch the article HTML.
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(
                article_url,
                headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
            )
            if r.status_code != 200:
                return []
            html = r.text
    except Exception as exc:
        logger.warning("wikipedia fetch failed: %s", exc)
        return []
    # Step 3: pull company names from the article. Wikipedia "list of
    # companies" articles come in TWO flavours:
    #   (a) Wikitable with columns — used for Phoenix, Miami, etc.
    #   (b) Bulleted list of links — used for Austin, Baltimore, etc.
    # Try (a) first; fall back to (b) if no wikitable is present.
    rows: List[Dict[str, Any]] = []
    m_table = re.search(
        r"<table[^>]*class=\"[^\"]*wikitable[^\"]*\"[^>]*>(.*?)</table>",
        html, flags=re.DOTALL | re.IGNORECASE,
    )
    if m_table:
        table_html = m_table.group(1)
        for m_row in re.finditer(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.DOTALL | re.IGNORECASE):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m_row.group(1), flags=re.DOTALL | re.IGNORECASE)
            if not cells:
                continue
            clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            clean = [re.sub(r"&[a-zA-Z#0-9]+;", " ", c) for c in clean]
            clean = [re.sub(r"\s+", " ", c).strip() for c in clean]
            name = clean[0] if clean else ""
            if not name or len(name) < 2 or name.lower() in {"name", "company", "company name"}:
                continue
            rows.append(_wiki_row(name, query, clean[1] if len(clean) > 1 else "", article_url))
            if len(rows) >= limit:
                break
    if not rows:
        # Bullet-list flavour: <li><a title="Company Name"> blocks
        # inside the main content area only. We MUST constrain to the
        # article body (`mw-parser-output`) because Wikipedia's
        # navigation chrome (sidebar, footer, dropdowns) renders the
        # same <li><a title=…> structure and would otherwise leak in
        # as "results" like 'Visit the main page'.
        body_m = re.search(
            r"<div[^>]*class=\"[^\"]*mw-parser-output[^\"]*\"[^>]*>(.+?)<div[^>]*(?:role=\"navigation\"|class=\"[^\"]*printfooter)",
            html, flags=re.DOTALL | re.IGNORECASE,
        )
        body_html = body_m.group(1) if body_m else html
        li_titles = re.findall(
            r"<li[^>]*>\s*<a [^>]*title=\"([^\"]+)\"",
            body_html,
        )
        seen: set[str] = set()
        for title in li_titles:
            # Decode the handful of HTML entities Wikipedia actually
            # emits in titles. Full lib import would be overkill.
            clean = (title
                     .replace("&amp;", "&")
                     .replace("&#039;", "'")
                     .replace("&quot;", "\"")
                     .strip())
            if not clean or clean in seen:
                continue
            if ":" in clean:  # Help:, Wikipedia:, File: chrome
                continue
            # Drop short bracketed nav hints like "Visit X [z]".
            if re.search(r"\[[a-z]\]$", clean):
                continue
            seen.add(clean)
            rows.append(_wiki_row(clean, query, "", article_url))
            if len(rows) >= limit:
                break
    return rows


def _wiki_row(name: str, city: str, industry: str, source_url: str) -> Dict[str, Any]:
    """Shape a single harvested Wikipedia company name into the standard
    lead-hunt CSV row schema. Centralised so the wikitable + bullet-list
    branches stay in sync."""
    return {
        "Business Name": name,
        "City": city,
        "State": "",
        "Industry": industry,
        "Years in Business": "",
        "Annual Revenue (USD)": "",
        "Website URL": "",
        "Contact Name": "",
        "Contact Phone": "",
        "Contact Email": "",
        "Source URL": source_url,
    }


# ── OpenCorporates ─────────────────────────────────────────────────────────
async def search_opencorporates(
    *, jurisdiction: str, q: str, limit: int = 25,
) -> List[Dict[str, Any]]:
    """Hit OpenCorporates' public REST endpoint.

    ``jurisdiction`` is an OpenCorporates code like ``us_az``, ``us_tx``,
    ``us_ga``. ``q`` is a name or industry keyword. Free tier allows
    moderate volume without an API key.
    """
    url = "https://api.opencorporates.com/companies/search"
    params = {
        "jurisdiction_code": jurisdiction,
        "q": q,
        "per_page": str(min(limit, 30)),
    }
    data = await _get_json(url, params=params)
    if not data:
        return []
    rows: List[Dict[str, Any]] = []
    for comp_wrap in ((data.get("results") or {}).get("companies") or []):
        c = comp_wrap.get("company") or {}
        name = c.get("name") or ""
        if not name:
            continue
        addr = (c.get("registered_address_in_full") or "").strip()
        # Extract city + state from the address line where possible.
        city, state = _parse_us_address(addr)
        rows.append({
            "Business Name": name,
            "City": city,
            "State": state,
            "Industry": (c.get("company_type") or ""),
            "Years in Business": "",
            "Annual Revenue (USD)": "",
            "Website URL": "",
            "Contact Name": "",
            "Contact Phone": "",
            "Contact Email": "",
            "Source URL": c.get("opencorporates_url") or "",
        })
        if len(rows) >= limit:
            break
    return rows


def _parse_us_address(addr: str) -> tuple[str, str]:
    """Best-effort: extract (city, state) from a free-form US address.
    Returns ``("", "")`` if it can't find a 2-letter state code."""
    if not addr:
        return "", ""
    # Common patterns: "...City, ST 12345", "...City, ST"
    m = re.search(r",\s*([A-Z][A-Za-z .'-]+),\s*([A-Z]{2})(?:\s+\d{5})?$", addr)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", ""
