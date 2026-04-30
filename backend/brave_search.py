"""Brave Search API helper.

A thin async wrapper around https://api.search.brave.com/res/v1/web
for use inside agent loops. Designed for low latency (<2s) and small
payloads — top-N web results with title, url, snippet, age.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional, TypedDict

import httpx

log = logging.getLogger("brave")

BRAVE_BASE = "https://api.search.brave.com/res/v1"
DEFAULT_TIMEOUT = 4.0   # generous for first-call cold paths
DEFAULT_COUNT = 5
DEFAULT_COUNTRY = "US"
DEFAULT_SAFESEARCH = "moderate"  # off | moderate | strict


class WebResult(TypedDict, total=False):
    title: str
    url: str
    snippet: str
    age: Optional[str]
    source: Optional[str]


def _api_key() -> Optional[str]:
    return os.environ.get("BRAVE_API_KEY") or None


async def brave_web_search(
    query: str,
    *,
    count: int = DEFAULT_COUNT,
    country: str = DEFAULT_COUNTRY,
    safesearch: str = DEFAULT_SAFESEARCH,
    timeout: float = DEFAULT_TIMEOUT,
    freshness: Optional[str] = None,  # 'pd'(day) | 'pw'(week) | 'pm'(month) | 'py'(year)
) -> List[WebResult]:
    """Return the top `count` web results for `query`. Empty list on any failure
    so the caller can degrade gracefully."""
    key = _api_key()
    if not key or not query.strip():
        return []
    params = {
        "q": query.strip(),
        "count": max(1, min(count, 20)),
        "country": country,
        "safesearch": safesearch,
        "spellcheck": "true",
    }
    if freshness:
        params["freshness"] = freshness

    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": key,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{BRAVE_BASE}/web/search", headers=headers, params=params)
        if resp.status_code != 200:
            log.warning("Brave search %s: %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Brave search error: %s", exc)
        return []

    web = (data.get("web") or {}).get("results") or []
    out: List[WebResult] = []
    for item in web[:count]:
        meta = item.get("meta_url") or {}
        out.append({
            "title": item.get("title", "").strip(),
            "url": item.get("url", "").strip(),
            "snippet": (item.get("description") or "").strip(),
            "age": item.get("age") or item.get("page_age") or None,
            "source": meta.get("hostname") or None,
        })
    return out


def format_results_for_prompt(results: List[WebResult]) -> str:
    """Compact, model-friendly rendering of search results."""
    if not results:
        return ""
    lines = ["Recent web search results (use these for fresh facts; cite the URL when relevant):"]
    for i, r in enumerate(results, 1):
        meta_bits = []
        if r.get("source"): meta_bits.append(r["source"])
        if r.get("age"):    meta_bits.append(r["age"])
        meta = f" [{' · '.join(meta_bits)}]" if meta_bits else ""
        lines.append(f"{i}. {r.get('title','(untitled)')}{meta}\n   {r.get('url','')}\n   {r.get('snippet','')}")
    return "\n".join(lines)
