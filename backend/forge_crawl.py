"""Forge Crawl — an in-house alternative to Firecrawl.

Scope of this module (Phase A):
  * ``scrape(url, render=…)`` — fetch a single page and return clean
    markdown + plain text + final URL + page title + status.
  * ``extract(url, schema, instructions=…)`` — scrape the page, then
    have an LLM coerce the content into a strict JSON object matching
    the caller-supplied JSON schema. Pure best-effort — we always
    return *something* JSON-parseable; the caller validates.
  * ``crawl(url, max_pages=…)`` — Phase A == shallow same-host crawl
    (depth 1: scrape the seed page, then scrape every same-host link
    we discover up to `max_pages`). Used by the audit pipeline to grab
    /about, /pricing, /contact alongside the homepage.

Why it exists:
  * Firecrawl is AGPL-licensed and a paid SaaS — both are deal-breakers
    for embedding inside a closed-source commercial product. We already
    have Playwright (for JS-rendered sites) and trafilatura (for clean
    HTML→Markdown). The missing piece is a unified, narrowly-scoped
    function the audit pipeline and mission tools can both call without
    having to plumb Playwright wiring themselves.

Strategy:
  * Try ``httpx`` first — it's cheap (~100ms) and handles the 80% of
    pages that render server-side.
  * Auto-detect when we need Playwright: too-short body, or markdown
    that looks like a JS-rendered shell (``<div id="root">…</div>``
    or repeated "Loading…" / "Enable JavaScript" markers).
  * Callers can override with ``render="force"`` (always Playwright)
    or ``render="never"`` (httpx-only).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

# Maximum bytes we'll keep from a single page. trafilatura already
# strips most chrome; this is a final guardrail so a 5 MB blog post
# doesn't blow up downstream LLM token budgets.
_MAX_MARKDOWN_CHARS = 80_000
# httpx fetch ceiling. Most well-behaved sites respond within 5s; the
# Playwright fallback covers the rest. Anything beyond 12s is almost
# certainly hostile or dead.
_HTTPX_TIMEOUT_S = 12.0
# Below this many cleaned-text characters we assume httpx got back a
# JS shell and escalate to Playwright (unless render="never").
_JS_RENDER_THRESHOLD = 400

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# A handful of "we 200-but-we're-actually-a-JS-shell" markers. The
# threshold above catches most of these on length alone; this regex
# is a belt-and-braces extra check.
_JS_SHELL_PATTERNS = (
    re.compile(r"<div\s+id=[\"']root[\"']\s*>\s*</div>", re.I),
    re.compile(r"<div\s+id=[\"']app[\"']\s*>\s*</div>", re.I),
    re.compile(r"enable\s+javascript\s+(to|and)", re.I),
    re.compile(r"you\s+need\s+to\s+enable\s+javascript", re.I),
)


def _normalize_url(url: str) -> str:
    """Make sure the URL has a scheme so httpx doesn't reject it."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def _looks_like_js_shell(html: str) -> bool:
    """Best-effort heuristic for "this HTML is just a JS bootstrap"."""
    if not html:
        return True
    sample = html[:6000]
    for pat in _JS_SHELL_PATTERNS:
        if pat.search(sample):
            return True
    return False


def _extract_markdown(html: str, *, url: str) -> Dict[str, Any]:
    """Pull main-content markdown + plain text + title out of HTML."""
    out: Dict[str, Any] = {"markdown": "", "text": "", "title": "", "links": []}
    if not html:
        return out
    try:
        # Imported lazily so module import doesn't crash on machines
        # where trafilatura is still being provisioned.
        import trafilatura
        from trafilatura.settings import use_config
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("trafilatura import failed: %s", exc)
        return out

    cfg = use_config()
    # Disable the per-process signal-based timeout — uvicorn runs
    # async workers off the main thread and signal.alarm only works on
    # the main thread, so leaving the default on raises ValueError.
    cfg.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")

    md = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_images=False,
        include_tables=True,
        favor_recall=True,
        config=cfg,
    ) or ""
    txt = trafilatura.extract(
        html,
        url=url,
        output_format="txt",
        include_links=False,
        include_images=False,
        favor_recall=True,
        config=cfg,
    ) or ""

    if len(md) > _MAX_MARKDOWN_CHARS:
        md = md[:_MAX_MARKDOWN_CHARS] + "\n\n…[truncated]"
    if len(txt) > _MAX_MARKDOWN_CHARS:
        txt = txt[:_MAX_MARKDOWN_CHARS] + "\n\n…[truncated]"

    # Cheap regex for <title>. trafilatura has a metadata extractor
    # too, but for our purposes the raw tag is fine and dependency-free.
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I | re.S)
    title = (m.group(1).strip() if m else "")[:300]

    out.update({"markdown": md.strip(), "text": txt.strip(), "title": title})
    out["links"] = _collect_links(html, base_url=url)
    return out


_LINK_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\']', re.I)


def _collect_links(html: str, *, base_url: str) -> List[str]:
    """All same-host absolute links in the page — used by the shallow
    crawler. Strips fragments and dedupes case-insensitively.
    """
    if not html:
        return []
    base = urlparse(base_url)
    base_host = base.hostname or ""
    out: List[str] = []
    seen: Set[str] = set()
    for href in _LINK_RE.findall(html):
        href = (href or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        # Only same-host, http(s) links — we intentionally ignore
        # subdomain expansion in Phase A.
        if parsed.scheme not in ("http", "https"):
            continue
        if (parsed.hostname or "") != base_host:
            continue
        normalized = parsed._replace(fragment="").geturl()
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
        if len(out) >= 200:
            break
    return out


async def _fetch_httpx(url: str) -> Dict[str, Any]:
    """Plain HTTP fetch. Returns a dict carrying the raw HTML so the
    caller can decide whether to escalate to Playwright."""
    try:
        async with httpx.AsyncClient(
            timeout=_HTTPX_TIMEOUT_S,
            follow_redirects=True,
            headers=_DEFAULT_HEADERS,
        ) as client:
            resp = await client.get(url)
        html = resp.text or ""
        return {
            "ok": resp.status_code < 400 and bool(html),
            "status": resp.status_code,
            "final_url": str(resp.url),
            "html": html,
            "error": "" if resp.status_code < 400 else f"HTTP {resp.status_code}",
        }
    except Exception as exc:
        return {"ok": False, "status": 0, "final_url": url, "html": "",
                "error": f"httpx error: {exc}"}


async def _fetch_playwright(url: str) -> Dict[str, Any]:
    """Playwright fallback — re-uses ``browser_tools.browse_url`` so we
    inherit its full stealth + Cloudflare-challenge handling. Returns
    a result-shaped dict that mirrors ``_fetch_httpx`` so the caller
    can branch uniformly.
    """
    try:
        from browser_tools import browse_url as _browse_url
    except Exception as exc:
        return {"ok": False, "status": 0, "final_url": url, "html": "",
                "error": f"playwright unavailable: {exc}"}
    try:
        # We need the rendered HTML, not just text — trafilatura works
        # on HTML. browse_url already returns rendered HTML via the
        # `html` key (it captures page.content() before close).
        snap = await _browse_url(url, take_screenshot=False)
    except Exception as exc:
        return {"ok": False, "status": 0, "final_url": url, "html": "",
                "error": f"playwright error: {exc}"}
    if snap.get("error"):
        return {"ok": False, "status": snap.get("status") or 0,
                "final_url": snap.get("final_url") or url, "html": "",
                "error": snap["error"]}
    html = snap.get("html") or ""
    # browse_url doesn't always populate `html`; fall back to a wrapped
    # version of the cleaned text so trafilatura at least sees *something*.
    if not html and snap.get("text"):
        text = snap["text"]
        # Wrap as minimal HTML so trafilatura's "favor_recall" path can
        # still find paragraphs. Better than returning empty.
        html = f"<html><body><pre>{text}</pre></body></html>"
    return {
        "ok": bool(html),
        "status": snap.get("status") or 200,
        "final_url": snap.get("final_url") or url,
        "html": html,
        "error": "" if html else "playwright returned no HTML",
    }


async def scrape(
    url: str,
    *,
    render: str = "auto",
) -> Dict[str, Any]:
    """Fetch ``url`` and return cleaned markdown + text + metadata.

    ``render`` controls Playwright usage:
      * ``"auto"`` (default) — httpx first; escalate to Playwright if
        the result looks like a JS shell.
      * ``"force"`` — always use Playwright (slower, more reliable).
      * ``"never"`` — httpx only; return whatever it can.

    Returned dict always carries ``ok``, ``error``, ``url``, ``final_url``,
    ``status``, ``title``, ``markdown``, ``text``, ``links``, ``engine``
    (``"httpx"`` or ``"playwright"``).
    """
    url = _normalize_url(url)
    if not url:
        return {"ok": False, "error": "url is required", "url": "",
                "final_url": "", "status": 0, "title": "", "markdown": "",
                "text": "", "links": [], "engine": ""}

    engine = "httpx"
    fetched: Dict[str, Any]
    if render == "force":
        engine = "playwright"
        fetched = await _fetch_playwright(url)
    else:
        fetched = await _fetch_httpx(url)
        # Decide whether to escalate. We escalate when:
        #   * httpx errored out (network, 4xx/5xx, etc.)
        #   * we got HTML but the cleaned text is very thin
        #   * the HTML looks like a JS shell
        if render != "never":
            need_escalate = False
            if not fetched.get("ok"):
                need_escalate = True
            else:
                # Cheap pre-check before doing the full markdown pass.
                # We only need an upper-bound here.
                html = fetched.get("html") or ""
                if _looks_like_js_shell(html):
                    need_escalate = True
                else:
                    # Run a quick markdown pass so the threshold check
                    # is on cleaned content, not raw HTML bytes.
                    quick = _extract_markdown(html, url=fetched["final_url"])
                    if len((quick.get("text") or "")) < _JS_RENDER_THRESHOLD:
                        need_escalate = True
                    else:
                        # We already have what we need — short-circuit so
                        # we don't pay for trafilatura twice.
                        return {
                            "ok": True, "error": "",
                            "url": url,
                            "final_url": fetched["final_url"],
                            "status": fetched["status"],
                            "engine": "httpx",
                            **quick,
                        }
            if need_escalate:
                logger.info("Forge Crawl: escalating %s to playwright", url)
                pw = await _fetch_playwright(url)
                if pw.get("ok"):
                    fetched = pw
                    engine = "playwright"

    if not fetched.get("ok"):
        return {"ok": False, "error": fetched.get("error") or "fetch failed",
                "url": url, "final_url": fetched.get("final_url") or url,
                "status": fetched.get("status") or 0, "title": "",
                "markdown": "", "text": "", "links": [], "engine": engine}

    parsed = _extract_markdown(fetched["html"], url=fetched["final_url"])
    return {
        "ok": True, "error": "",
        "url": url,
        "final_url": fetched["final_url"],
        "status": fetched["status"],
        "engine": engine,
        **parsed,
    }


# ── Shallow crawl ─────────────────────────────────────────────────────────

# Path tokens we ALWAYS prioritise when crawling a marketing site —
# these are the pages that carry the brand promise, pricing, and
# customer trust signals. The audit pipeline benefits most from them.
_PRIORITY_PATH_TOKENS = (
    "/about", "/pricing", "/plans", "/products", "/services",
    "/contact", "/team", "/faq", "/customers", "/case",
)


def _rank_links(seed: str, links: List[str], *, max_pages: int) -> List[str]:
    """Reorder discovered links so high-signal pages get crawled first."""
    seed_parsed = urlparse(seed)
    seed_path = seed_parsed.path.rstrip("/").lower()

    def _score(url: str) -> int:
        path = urlparse(url).path.lower()
        if not path or path == "/":
            return -10  # already the seed
        score = 0
        for token in _PRIORITY_PATH_TOKENS:
            if token in path:
                score += 50
                break
        # Penalise deep paths and obvious blog/article URLs — they're
        # rarely useful for a business audit.
        depth = path.strip("/").count("/")
        score -= depth * 5
        if "/blog/" in path or "/news/" in path or "/post/" in path:
            score -= 20
        if path == seed_path:
            score -= 100
        return score

    ranked = sorted(links, key=_score, reverse=True)
    # De-dupe while preserving order, then cap.
    seen: Set[str] = set()
    out: List[str] = []
    for u in ranked:
        key = u.split("#")[0].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
        if len(out) >= max_pages:
            break
    return out


async def crawl(
    url: str,
    *,
    max_pages: int = 5,
    render: str = "auto",
) -> Dict[str, Any]:
    """Shallow same-host crawl. Scrapes the seed page, then up to
    ``max_pages - 1`` additional same-host links discovered on it.

    Phase A scope: depth 1 only. Phase B / C will add BFS to depth N,
    a per-domain robots.txt check, and sitemap.xml priming.
    """
    max_pages = max(1, min(int(max_pages or 5), 20))
    seed = await scrape(url, render=render)
    pages: List[Dict[str, Any]] = [seed]
    if not seed.get("ok"):
        return {"ok": False, "error": seed.get("error") or "seed fetch failed",
                "url": url, "pages": pages, "engine": seed.get("engine") or ""}

    remaining = max_pages - 1
    if remaining <= 0:
        return {"ok": True, "error": "", "url": url, "pages": pages,
                "engine": seed.get("engine") or ""}

    candidates = _rank_links(seed["final_url"], seed.get("links") or [],
                             max_pages=remaining)
    # Fetch in parallel — same-host, polite ceiling.
    results = await asyncio.gather(
        *[scrape(u, render=render) for u in candidates],
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            pages.append({"ok": False, "error": f"{r}", "url": "",
                          "final_url": "", "status": 0, "title": "",
                          "markdown": "", "text": "", "links": [],
                          "engine": ""})
        else:
            pages.append(r)
    return {"ok": True, "error": "", "url": url, "pages": pages,
            "engine": seed.get("engine") or ""}


# ── Structured extraction ────────────────────────────────────────────────

_EXTRACT_SYSTEM_PROMPT = (
    "You are a precise information extractor. Given a web page's "
    "markdown content and a JSON schema describing the fields the "
    "caller needs, you return ONE JSON object that conforms to the "
    "schema. Rules:\n"
    "  • Return ONLY a JSON object — no prose, no markdown fences.\n"
    "  • If a field is genuinely absent from the content, use null "
    "(for scalars) or [] (for lists). Never invent.\n"
    "  • Prefer verbatim phrasing from the page over your own summary.\n"
    "  • If the schema specifies an array of objects, return up to the "
    "ten most relevant items.\n"
)


def _strip_json_fences(s: str) -> str:
    """Best-effort JSON unwrap. LLMs love wrapping JSON in ```json …```
    despite explicit instructions not to."""
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


async def extract(
    url: str,
    *,
    schema: Dict[str, Any],
    instructions: str = "",
    render: str = "auto",
    llm_chat_cls=None,
    user_message_cls=None,
    emergent_llm_key: str = "",
    model_provider: str = "openai",
    model_name: str = "gpt-4o-mini",
) -> Dict[str, Any]:
    """Scrape ``url``, then ask an LLM to coerce the content into the
    caller-supplied JSON schema.

    The caller injects ``LlmChat`` / ``UserMessage`` / the Emergent key
    so this module stays decoupled from the rest of the backend's LLM
    plumbing.
    """
    if not llm_chat_cls or not user_message_cls or not emergent_llm_key:
        return {"ok": False, "error": "LLM dependencies not provided",
                "url": url, "data": None}

    page = await scrape(url, render=render)
    if not page.get("ok"):
        return {"ok": False, "error": page.get("error") or "scrape failed",
                "url": url, "data": None, "page": page}

    # Trim markdown so we stay well within model context. The schema
    # itself is also serialised compactly.
    content = page.get("markdown") or page.get("text") or ""
    if len(content) > 24_000:
        content = content[:24_000] + "\n\n…[truncated]"

    user_payload = (
        f"# Page URL\n{page['final_url']}\n\n"
        f"# Page Title\n{page.get('title') or '(none)'}\n\n"
        f"# Extraction instructions\n{instructions or '(none — use the schema as the only spec)'}\n\n"
        f"# JSON schema (keys + expected types)\n```json\n"
        f"{json.dumps(schema, ensure_ascii=False)}\n```\n\n"
        f"# Page markdown\n{content}\n\n"
        f"Return ONLY a JSON object matching the schema."
    )

    chat = llm_chat_cls(
        api_key=emergent_llm_key,
        session_id=f"forge-extract:{page['final_url'][:80]}",
        system_message=_EXTRACT_SYSTEM_PROMPT,
    ).with_model(model_provider, model_name)

    try:
        raw = await asyncio.wait_for(
            chat.send_message(user_message_cls(text=user_payload)),
            timeout=45.0,
        )
    except Exception as exc:
        return {"ok": False, "error": f"LLM call failed: {exc}",
                "url": url, "data": None, "page": page}

    raw = _strip_json_fences(raw or "")
    try:
        data = json.loads(raw)
    except Exception as exc:
        # Last-ditch — try to locate the first {...} blob in the response.
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                return {"ok": False,
                        "error": f"LLM returned unparseable JSON: {exc}",
                        "url": url, "data": None, "raw": raw[:1000],
                        "page": page}
        else:
            return {"ok": False,
                    "error": f"LLM returned no JSON: {exc}",
                    "url": url, "data": None, "raw": raw[:1000],
                    "page": page}

    return {"ok": True, "error": "", "url": url, "final_url": page["final_url"],
            "data": data, "engine": page.get("engine") or ""}
