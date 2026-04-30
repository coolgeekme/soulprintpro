"""Perplexity Sonar — fallback for URL scraping & Brave web search.

Two helpers:

- ``perplexity_company_summary(name, url, max_chars)`` returns a compact text
  blob describing the company, sourced from Perplexity's live web search.
  Used by the Soul generator when our httpx scraper can't read the site
  (Cloudflare 403, JS-only SPAs, DNS issues, …).

- ``perplexity_web_search(query, count, freshness)`` returns the same
  ``WebResult`` shape as ``brave_search.brave_web_search`` so it drops in as
  a seamless fallback when Brave returns 0 results or rate-limits.

Both return safe empty values on any failure — callers degrade gracefully.
"""
from __future__ import annotations

import logging
import os
import re
from typing import List, Optional

import httpx

log = logging.getLogger("perplexity")

PPLX_BASE = "https://api.perplexity.ai"
DEFAULT_MODEL = "sonar"
DEFAULT_TIMEOUT = 25.0  # Sonar can be slow; URL scrape fallback can wait


def _api_key() -> Optional[str]:
    key = os.environ.get("PERPLEXITY_API_KEY") or ""
    key = key.strip()
    return key or None


def is_available() -> bool:
    return _api_key() is not None


async def _chat(
    messages: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT,
    max_tokens: int = 800,
    search_recency: Optional[str] = None,
) -> tuple[str, list[dict]]:
    """Low-level POST. Returns ``(content, search_results)`` or ``("", [])`` on
    failure. ``search_results`` are dicts with title/url/snippet/date when
    Perplexity provides them, plus we fold ``citations`` URLs in too.
    """
    key = _api_key()
    if not key:
        return "", []
    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    if search_recency:
        payload["search_recency_filter"] = search_recency
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{PPLX_BASE}/chat/completions", headers=headers, json=payload
            )
        if resp.status_code != 200:
            log.warning("Perplexity %s: %s", resp.status_code, resp.text[:300])
            return "", []
        data = resp.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Perplexity error: %s", exc)
        return "", []

    try:
        content = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        content = ""

    # Newer responses use `search_results`; older ones only `citations`.
    raw_results = data.get("search_results") or []
    citations = data.get("citations") or []
    seen: set[str] = set()
    results: list[dict] = []
    for item in raw_results:
        url = (item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            "title": (item.get("title") or "").strip(),
            "url": url,
            "snippet": (item.get("snippet") or item.get("description") or "").strip(),
            "date": item.get("date") or item.get("last_updated"),
        })
    for url in citations:
        if isinstance(url, str) and url and url not in seen:
            seen.add(url)
            results.append({"title": "", "url": url, "snippet": "", "date": None})
    return content, results


# ── Use case 1: Soul-generator fallback ──────────────────────────────────────
async def perplexity_company_summary(
    name: str, url: str = "", *, max_chars: int = 4000
) -> str:
    """Return plain-text context about the company sourced from Perplexity.

    Empty string on failure (no key, network, parse). The caller (`server.py`
    `_generate_company_soul`) treats this just like a successful website
    scrape.
    """
    if not is_available():
        return ""
    name = (name or "").strip()
    if not name and not url:
        return ""
    bits: list[str] = []
    if name:
        bits.append(f"Company name: {name}")
    if url:
        bits.append(f"Website: {url}")
    bits.append(
        "In 4–6 short paragraphs, describe what this company does, who its "
        "customers are, the tone of its public-facing copy, its core values, "
        "and any clear long-term ambition. Quote phrases from their own site "
        "where helpful. Don't include marketing fluff or your own opinions."
    )
    user_msg = "\n".join(bits)
    content, _results = await _chat(
        [
            {"role": "system", "content": "You are a precise business research analyst."},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=900,
    )
    if not content:
        return ""
    # Strip Perplexity's bracketed citation tokens like [1], [2][3] — they
    # confuse the downstream Soul-extractor LLM and aren't useful here.
    cleaned = re.sub(r"\[\d+(?:\]\[\d+)*\]", " ", content)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_chars]


# ── Use case 2: Brave-search drop-in fallback ────────────────────────────────
# Returns the same shape as `brave_search.WebResult` so callers can swap in.
async def perplexity_web_search(
    query: str,
    *,
    count: int = 5,
    freshness: Optional[str] = None,  # 'pd' | 'pw' | 'pm' | 'py' (Brave codes)
) -> List[dict]:
    """Same shape as ``brave_search.brave_web_search`` — empty list on failure."""
    if not is_available() or not query.strip():
        return []
    # Brave uses pd/pw/pm/py — Perplexity uses day/week/month/year.
    recency_map = {"pd": "day", "pw": "week", "pm": "month", "py": "year"}
    recency = recency_map.get(freshness or "")

    messages = [
        {
            "role": "system",
            "content": (
                "You are a web search engine. Reply with a 2–3 sentence "
                "factual answer to the query, citing sources inline. The "
                "user only sees your citations, not the prose."
            ),
        },
        {"role": "user", "content": query.strip()},
    ]
    content, raw_results = await _chat(
        messages, max_tokens=400, search_recency=recency,
    )
    # Perplexity's recency filter is sometimes stricter than Brave's — if it
    # returns nothing with recency, retry without to guarantee at least the
    # zero-result-Brave fallback yields *something*.
    if not raw_results and recency:
        content, raw_results = await _chat(messages, max_tokens=400)

    out: List[dict] = []
    for r in raw_results[:count]:
        url = r.get("url") or ""
        host = ""
        if url:
            try:
                from urllib.parse import urlparse
                host = (urlparse(url).hostname or "").replace("www.", "")
            except Exception:
                host = ""
        title = r.get("title") or host or "(untitled)"
        snippet = r.get("snippet") or ""
        # If we have no per-result snippet but Perplexity gave a synthesised
        # answer, attach it to the first result so the caller has *something*
        # to feed into the agent prompt.
        if not snippet and not out and content:
            snippet = content[:280]
        out.append({
            "title": title.strip()[:200],
            "url": url,
            "snippet": snippet.strip()[:400],
            "age": None,
            "source": host or None,
        })
    return out
