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
        # Capture token counts from the API response so the admin
        # dashboard reflects real per-call cost (Perplexity bills
        # tokens AND a per-request fee).
        ok = resp.status_code == 200
        usage = {}
        try:
            usage = (resp.json() or {}).get("usage") or {} if ok else {}
        except Exception:
            pass
        try:
            from external_api_costs import log_external_call_fire_and_forget
            # Match provider key to model (sonar-pro vs sonar plain)
            prov = "perplexity.sonar-pro" if "pro" in (model or "").lower() else "perplexity.sonar"
            log_external_call_fire_and_forget(
                provider=prov, kind_label="research_synth",
                calls=1, success=ok,
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
                error=None if ok else f"HTTP {resp.status_code}",
            )
        except Exception:
            pass
        if not ok:
            log.warning("Perplexity %s: %s", resp.status_code, resp.text[:300])
            return "", []
        data = resp.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        try:
            from external_api_costs import log_external_call_fire_and_forget
            prov = "perplexity.sonar-pro" if "pro" in (model or "").lower() else "perplexity.sonar"
            log_external_call_fire_and_forget(
                provider=prov, kind_label="research_synth",
                calls=1, success=False, error=str(exc)[:200],
            )
        except Exception:
            pass
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

    Empty string on failure (no key, network, parse, or the URL is genuinely
    not in Perplexity's index — we DELIBERATELY don't accept descriptions of
    similarly-named-but-different sites). The caller (`server.py`
    `_generate_company_soul`) treats this just like a successful website
    scrape.
    """
    if not is_available():
        return ""
    name = (name or "").strip()
    url = (url or "").strip()
    if not name and not url:
        return ""
    # Strict prompt: the URL is the ground truth, not the name. Lots of
    # `.app` / `.io` startups share names with established `.com` companies
    # (e.g. playtrackr.app vs playtrackr.com); without this guardrail
    # Perplexity will happily describe the wrong one.
    bits: list[str] = []
    if url:
        bits.append(f"Website (ground truth — describe ONLY this exact URL): {url}")
    if name:
        bits.append(f"Company name (may overlap with unrelated sites): {name}")
    bits.append(
        "In 4–6 short paragraphs, describe ONLY what the website at the URL "
        "above does — its actual product, customers, tone, values, and "
        "ambition. Do NOT describe a different site that happens to share the "
        "name. Quote phrases from the site itself where helpful.\n\n"
        "If you do NOT have specific information about the exact URL above "
        "(not a similar-name site, the EXACT URL), respond with the single "
        "word UNKNOWN and nothing else."
    )
    user_msg = "\n".join(bits)
    content, _results = await _chat(
        [
            {"role": "system", "content": "You are a precise business research analyst. Stay grounded on the exact URL provided; never substitute a similarly-named site."},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=900,
    )
    if not content:
        return ""
    # Strip Perplexity's bracketed citation tokens like [1], [2][3]
    cleaned = re.sub(r"\[\d+(?:\]\[\d+)*\]", " ", content)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Detect the "UNKNOWN" response (sometimes wrapped in quotes/markdown)
    if re.fullmatch(r"[\"'`*_ \t]*UNKNOWN[\"'`*_ \t.!,]*", cleaned, flags=re.IGNORECASE):
        log.info("Perplexity returned UNKNOWN for %s", url or name)
        return ""
    # Extra safety net: if the response doesn't even mention the URL host or
    # the company name, it's almost certainly hallucinating about a different
    # site — discard it. We require at least one anchor token.
    anchors = []
    if url:
        try:
            from urllib.parse import urlparse
            host = (urlparse(url if url.startswith(("http://", "https://")) else "https://" + url).hostname or "").lower()
            if host:
                anchors.append(host.replace("www.", ""))
                # also accept the bare domain stem (e.g. "playtrackr")
                stem = host.split(".")[0] if host else ""
                if stem and len(stem) >= 4:
                    anchors.append(stem)
        except Exception:
            pass
    if name and len(name) >= 3:
        anchors.append(name.lower())
    if anchors:
        low = cleaned.lower()
        if not any(a in low for a in anchors):
            log.info("Perplexity result didn't mention %s — discarding as off-target", anchors)
            return ""
    return cleaned[:max_chars]


# ── Use case 3: Social profile style analysis ────────────────────────────────
async def perplexity_analyze_social_profile(
    platform: str, url: str, *, handle: str = "", role: str = "own",
    max_chars: int = 3500,
) -> str:
    """Return a plain-text analysis of a public social profile's *style*.

    Used by the Social Style Guide synthesizer. Empty string on failure
    (rate-limit, profile not in Perplexity's index, blocked profile, etc.).
    The caller treats this like any other text source for downstream LLM
    style synthesis.
    """
    if not is_available():
        return ""
    platform = (platform or "").strip() or "social"
    url = (url or "").strip()
    handle = (handle or "").strip()
    if not url and not handle:
        return ""
    role_label = "the user's own profile" if role == "own" else "an inspiration profile to emulate"
    bits = [f"Platform: {platform}"]
    if url:
        bits.append(f"Profile URL (ground truth — analyze ONLY this exact account): {url}")
    if handle:
        bits.append(f"Handle: {handle}")
    bits.append(f"Role: {role_label}")
    bits.append(
        "Analyze the most recent 10–20 public posts from this account and "
        "produce a precise STYLE TEARDOWN covering:\n"
        "1. Tone & voice (3–5 adjectives + a one-sentence gestalt).\n"
        "2. Vocabulary patterns — favorite phrases, signature words, slang, "
        "and words/phrases the account avoids.\n"
        "3. Post structure — typical length, hook patterns, body shape, CTA "
        "patterns, hashtag usage, emoji usage, line breaks.\n"
        "4. Recurring themes / topics that perform well.\n"
        "5. Visual style of attached images/videos — color palette, "
        "composition, illustration vs photo, motion if videos, on-image "
        "typography.\n"
        "6. Posting cadence and best-performing post types if visible.\n"
        "7. 2–3 verbatim sample posts (short quotes, exact wording).\n\n"
        "Be specific and concrete — quote phrases, name colors, count "
        "hashtags. If you cannot access this exact profile, respond with "
        "the single word UNKNOWN and nothing else."
    )
    user_msg = "\n".join(bits)
    # Use ``sonar-pro`` here — the base ``sonar`` model often refuses social
    # profile queries outright (returns "no data"), while sonar-pro reliably
    # pulls from mirrored posts / third-party roundups / news coverage.
    content, _results = await _chat(
        [
            {"role": "system", "content": (
                "You are a senior social-media brand strategist. Stay grounded "
                "on the exact profile provided; never substitute a similarly-"
                "named account. Be concrete, never generic. If you don't have "
                "the exact account's data, infer carefully from any mirrored "
                "posts, news coverage, or the brand's own website style — "
                "never respond with UNKNOWN; always produce your best "
                "publicly-grounded teardown."
            )},
            {"role": "user", "content": user_msg},
        ],
        model="sonar-pro",
        max_tokens=1200,
    )
    if not content:
        return ""
    cleaned = re.sub(r"\[\d+(?:\]\[\d+)*\]", " ", content)
    cleaned = re.sub(r"\s+\n", "\n", cleaned).strip()
    # Legacy guard — kept for defensive cleanup even though the updated
    # prompt tells the model never to emit UNKNOWN.
    if re.fullmatch(r"[\"'`*_ \t]*UNKNOWN[\"'`*_ \t.!,]*", cleaned, flags=re.IGNORECASE):
        log.info("Perplexity returned UNKNOWN for social profile %s", url or handle)
        return ""
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
