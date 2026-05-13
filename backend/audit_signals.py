"""Hard-data signal collectors for the Business Audit pipeline.

Where the rest of the audit relies on LLM judgement over scraped text + web
search, this module gathers **objective signals** — numbers, screenshots, DNS
records — that no chat agent can replicate in a 30-second response. These
become the "investigation-grade prospect dossier" backbone of the audit.

All four collectors:
  • run independently (you can ship one without the others)
  • return a serializable dict shaped for direct insertion into the audit doc
  • degrade gracefully — failures return ``{"available": False, "error": ...}``
    rather than raising, so a single broken signal can't take the audit down

Collectors:
  1. ``fetch_web_vitals(url)``            — Google PageSpeed Insights v5
  2. ``capture_viewport_screenshots(url)``— Playwright at 3 viewports
  3. ``inventory_tracking_pixels(url)``   — DOM/HTML sniff for known tags
  4. ``domain_reputation(url)``           — SPF / DKIM / DMARC + domain age

The audit pipeline calls all four in parallel from ``audits.py`` so the
wall-time cost is the slowest one, not the sum.
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


# ── 1. Core Web Vitals via Google PageSpeed Insights ───────────────────────
# v5 is the current stable API. No key needed for low-volume calls; you can
# set ``PAGESPEED_API_KEY`` in the env if you ever hit the free-tier quota.
# We pull both mobile and desktop strategies — Google's own ranking signal
# is mobile-first, but desktop scores matter for B2B prospects who pitch
# from a laptop. ~3-8 s typical wall time; capped at 25 s.
_PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
# 50s per single strategy. Standalone calls return in 3-9s, but when the
# audit pipeline runs the request concurrently with Playwright + 3 Brave
# searches + Perplexity + viewport screenshots + DNS lookups, HTTP socket
# contention can stretch a single PSI call to 30-40s. The audit-level
# bound on `fetch_web_vitals` (60s, in audits.py) is the real ceiling;
# this just lets a single strategy ride it out instead of failing early.
_PSI_TIMEOUT = 50.0


async def fetch_web_vitals(url: str, *, api_key: Optional[str] = None) -> Dict[str, Any]:
    """Return Core Web Vitals + performance score for ``url``.

    Output shape:
      {
        "available": True,
        "mobile":  {"performance": 73, "lcp_ms": 2400, "inp_ms": 180,
                     "cls": 0.04, "ttfb_ms": 420, "fcp_ms": 1800,
                     "total_blocking_time_ms": 220, "speed_index_ms": 3100},
        "desktop": {... same keys ...},
        "fetched_at": "2026-05-13T01:23:45Z",
      }

    On failure (network, quota, blocked URL) returns ``{"available": False,
    "error": "..."}``. Never raises.

    NOTE on the API key: the unauthenticated PSI quota is ~25 calls/day
    *per IP* — fine for local development but useless in production.
    Set ``PAGESPEED_API_KEY`` in the backend env (free tier: 25 000
    queries/day) and we'll pick it up automatically. Get one at
    https://developers.google.com/speed/docs/insights/v5/get-started#APIKey
    """
    if not url:
        return {"available": False, "error": "no url"}
    if api_key is None:
        import os
        api_key = os.environ.get("PAGESPEED_API_KEY") or None

    async def _one(strategy: str) -> Dict[str, Any]:
        params = {
            "url": url,
            "strategy": strategy,
            "category": "performance",
        }
        if api_key:
            params["key"] = api_key
        import time
        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=_PSI_TIMEOUT) as client:
                r = await client.get(_PSI_URL, params=params)
                r.raise_for_status()
                data = r.json()
            logger.info("PSI %s for %s OK in %.1fs", strategy, url, time.time() - t0)
        except Exception as exc:
            logger.warning(
                "PSI %s for %s failed in %.1fs: %s",
                strategy, url, time.time() - t0, repr(exc)[:200],
            )
            raise
        lhr = data.get("lighthouseResult") or {}
        audits = lhr.get("audits") or {}
        cats = lhr.get("categories") or {}
        perf = ((cats.get("performance") or {}).get("score") or 0) * 100

        def _num(metric_key: str) -> Optional[float]:
            m = audits.get(metric_key) or {}
            v = m.get("numericValue")
            return round(float(v), 2) if isinstance(v, (int, float)) else None

        return {
            "performance": int(round(perf)),
            "lcp_ms": _num("largest-contentful-paint"),
            "inp_ms": _num("interactive"),  # PSI v5 uses 'interactive' for INP-equivalent
            "cls":    _num("cumulative-layout-shift"),
            "ttfb_ms": _num("server-response-time"),
            "fcp_ms": _num("first-contentful-paint"),
            "total_blocking_time_ms": _num("total-blocking-time"),
            "speed_index_ms": _num("speed-index"),
        }

    try:
        # Google rate-limits aggressive concurrent calls from the same
        # API key. Sequential mobile-then-desktop reliably returns each
        # in 3-9s (~6-18s total); parallel calls stall both at the
        # full read-timeout because PSI queues the second one behind
        # the first internally anyway. Sequential is strictly faster.
        try:
            mobile = await _one("mobile")
        except Exception as exc:
            mobile = exc
        try:
            desktop = await _one("desktop")
        except Exception as exc:
            desktop = exc
        if isinstance(mobile, Exception) and isinstance(desktop, Exception):
            msg = repr(mobile)
            if "429" in msg or "rateLimit" in msg or "quota" in msg.lower():
                msg = "PageSpeed Insights quota exceeded — set PAGESPEED_API_KEY in backend env"
            elif "Timeout" in msg:
                msg = "PageSpeed Insights timed out — site may be too slow to audit"
            return {"available": False, "error": msg[:300]}
        out: Dict[str, Any] = {
            "available": True,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        if not isinstance(mobile, Exception):
            out["mobile"] = mobile
        if not isinstance(desktop, Exception):
            out["desktop"] = desktop
        return out
    except Exception as exc:
        logger.warning("PageSpeed Insights fetch failed for %s: %s", url, exc)
        return {"available": False, "error": repr(exc)[:200] or "unknown PSI error"}


# ── 2. Multi-viewport screenshots ──────────────────────────────────────────
# Three standard breakpoints. We reuse the hardened Playwright context from
# ``browser_tools`` so anti-bot walls behave the same as the main fetch.
# Each screenshot is JPEG-encoded (~150-400 KB) and persisted via the
# caller's ``persist_bytes`` so we end up with public URLs the audit page
# and PDF can both embed.
_VIEWPORTS = [
    {"name": "mobile",  "width": 390,  "height": 844, "scale": 2},   # iPhone 14
    {"name": "tablet",  "width": 820,  "height": 1180, "scale": 2},  # iPad Air
    {"name": "desktop", "width": 1440, "height": 900, "scale": 1},   # MacBook
]


async def capture_viewport_screenshots(
    url: str,
    *,
    persist_bytes,
    upload_dir: Path,
    audit_id: str,
) -> Dict[str, Any]:
    """Snapshot ``url`` at mobile / tablet / desktop viewports — IN PARALLEL.

    Original implementation ran the three viewports sequentially: ~15s
    per viewport × 3 = ~45s of the audit's 240s watchdog budget. That
    pushed slow sites over the time limit. Now each viewport runs in
    its own asyncio task on a fresh Playwright context, so wall time
    collapses to ~the slowest single viewport (~15s).

    Returns:
      {
        "available": True,
        "shots": [
          {"viewport": "mobile",  "width": 390,  "url": "/api/files/audit-..."}
          {"viewport": "tablet",  "width": 820,  "url": "/api/files/audit-..."}
          {"viewport": "desktop", "width": 1440, "url": "/api/files/audit-..."}
        ]
      }
    """
    if not url:
        return {"available": False, "error": "no url"}
    try:
        from browser_tools import _ensure_browser, _hardened_context_kwargs, apply_stealth_init
    except Exception as exc:
        return {"available": False, "error": f"playwright not ready: {exc}"}

    try:
        browser = await _ensure_browser()
    except Exception as exc:
        return {"available": False, "error": f"browser unavailable: {exc}"}

    async def _one(vp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        ctx = None
        try:
            kwargs = _hardened_context_kwargs()
            kwargs["viewport"] = {"width": vp["width"], "height": vp["height"]}
            kwargs["device_scale_factor"] = vp["scale"]
            ctx = await browser.new_context(**kwargs)
            await apply_stealth_init(ctx)
            page = await ctx.new_page()
            # Tightened goto/load timeouts: 15s + 4s. A slow site shouldn't
            # eat 20+ seconds per viewport — we'd rather drop one viewport
            # than blow the watchdog budget.
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            try:
                await page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            png = await page.screenshot(type="jpeg", quality=72, full_page=False)
            # Filename uses the viewport WIDTH (digits-only) so it matches
            # the strict `ALLOWED_FILENAME_RE` regex in server.py that
            # only permits hex + dash. Semantic viewport name lives in
            # the returned dict so the frontend can label the card.
            fname = f"audit-{audit_id}-{vp['width']}.jpg"
            path = upload_dir / fname
            await persist_bytes(path, png, original_name=fname, mime="image/jpeg")
            return {
                "viewport": vp["name"],
                "width": vp["width"],
                "height": vp["height"],
                "url": f"/api/files/{fname}",
                "filename": fname,
            }
        except Exception as exc:
            logger.info("screenshot at %s failed for %s: %s", vp["name"], url, exc)
            return None
        finally:
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    pass

    results = await asyncio.gather(*(_one(vp) for vp in _VIEWPORTS))
    shots = [r for r in results if r is not None]
    if not shots:
        return {"available": False, "error": "no viewports captured"}
    return {"available": True, "shots": shots}


# ── 3. Tracking-pixel inventory ────────────────────────────────────────────
# Pattern-match the page's HTML + outbound script URLs for known analytics
# and ad-tech vendors. Each match emits ``{vendor, evidence, category}`` so
# the audit can say "you have GA4 but no Meta Pixel — agencies leading
# with paid social need both". Categories let us call out gaps later
# ("you're not measuring conversion at all").
_PIXEL_PATTERNS = [
    # Analytics
    ("Google Analytics 4",    "analytics",     re.compile(r"\bG-[A-Z0-9]{8,12}\b|gtag/js\?id=G-|googletagmanager\.com/gtag/js")),
    ("Google Universal Analytics (legacy)", "analytics", re.compile(r"\bUA-\d{4,}-\d+\b|google-analytics\.com/analytics\.js")),
    ("Google Tag Manager",    "tag-manager",   re.compile(r"\bGTM-[A-Z0-9]{4,}\b|googletagmanager\.com/gtm\.js")),
    ("Microsoft Clarity",     "session-replay",re.compile(r"clarity\.ms/tag/|window\.clarity")),
    ("Hotjar",                "session-replay",re.compile(r"hotjar\.com|static\.hotjar|hjid:")),
    ("FullStory",             "session-replay",re.compile(r"fullstory\.com|edge\.fullstory\.com")),
    ("Mixpanel",              "product-analytics", re.compile(r"cdn\.mxpnl\.com|mixpanel\.init\(")),
    ("Amplitude",             "product-analytics", re.compile(r"amplitude\.com|cdn\.amplitude\.com")),
    ("Segment",               "cdp",           re.compile(r"cdn\.segment\.com|analytics\.load\(|\banalytics\.identify\b")),
    ("PostHog",               "product-analytics", re.compile(r"posthog\.com/static|posthog\.init")),
    # Ad pixels
    ("Meta Pixel (Facebook)", "ads",           re.compile(r"connect\.facebook\.net/.*fbevents|fbq\(['\"]init['\"]")),
    ("LinkedIn Insight Tag",  "ads",           re.compile(r"snap\.licdn\.com/li\.lms-analytics|_linkedin_partner_id")),
    ("Twitter / X Pixel",     "ads",           re.compile(r"static\.ads-twitter\.com/uwt\.js|twq\(")),
    ("TikTok Pixel",          "ads",           re.compile(r"analytics\.tiktok\.com/i18n/pixel|ttq\.load")),
    ("Reddit Pixel",          "ads",           re.compile(r"www\.redditstatic\.com/ads/pixel\.js|rdt\(")),
    ("Pinterest Tag",         "ads",           re.compile(r"s\.pinimg\.com/ct/core\.js|pintrk\(")),
    ("Snap Pixel",            "ads",           re.compile(r"sc-static\.net/scevent\.min\.js|snaptr\(")),
    ("Google Ads Conversion", "ads",           re.compile(r"googleadservices\.com/pagead/conversion|AW-\d+")),
    # Chat / Support
    ("Intercom",              "chat",          re.compile(r"widget\.intercom\.io|window\.Intercom\(")),
    ("Drift",                 "chat",          re.compile(r"js\.driftt\.com|drift\.t")),
    ("HubSpot",               "marketing-automation", re.compile(r"js\.hs-scripts\.com|js\.hubspot\.com|_hsq\.push")),
    ("Marketo",               "marketing-automation", re.compile(r"munchkin\.marketo|MunchkinTracker")),
    ("Pardot / MCAE",         "marketing-automation", re.compile(r"pi\.pardot\.com|piTracker")),
    # Heat / experiments
    ("Optimizely",            "experiments",   re.compile(r"cdn\.optimizely\.com/js|optimizely\.com/datafiles")),
    ("VWO",                   "experiments",   re.compile(r"dev\.visualwebsiteoptimizer\.com|_vwo_code")),
    # Privacy
    ("OneTrust (consent)",    "consent",       re.compile(r"cdn\.cookielaw\.org|otSDKStub\.js")),
    ("Cookiebot",             "consent",       re.compile(r"consent\.cookiebot\.com")),
]


def inventory_tracking_pixels(html: str) -> Dict[str, Any]:
    """Scan rendered HTML for known tracking / analytics / ad pixel vendors.

    Synchronous on purpose — pattern matching is CPU-bound, no IO.
    """
    if not html:
        return {"available": False, "error": "no html"}
    found: List[Dict[str, str]] = []
    seen: set = set()
    for vendor, category, pattern in _PIXEL_PATTERNS:
        m = pattern.search(html)
        if m and vendor not in seen:
            seen.add(vendor)
            found.append({
                "vendor": vendor,
                "category": category,
                "evidence": (m.group(0) or "")[:80],
            })

    # Derive simple "what's missing" gaps the audit can call out.
    categories_present = {f["category"] for f in found}
    gaps: List[str] = []
    if "analytics" not in categories_present and "product-analytics" not in categories_present:
        gaps.append("No web analytics detected — you can't optimize what you don't measure.")
    if "ads" not in categories_present:
        gaps.append("No ad-tracking pixels detected — paid traffic from any channel will be invisible to your team.")
    if "consent" not in categories_present and any(c in categories_present for c in ("analytics", "ads", "session-replay")):
        gaps.append("Tracking installed without a consent banner — review GDPR / CCPA exposure.")

    return {
        "available": True,
        "count": len(found),
        "pixels": found,
        "gaps": gaps,
    }


# ── 4. Domain / email reputation ───────────────────────────────────────────
# SPF, DKIM (well-known selectors only — there are infinite selectors so we
# probe the common ones), DMARC, MX presence. All synchronous DNS calls
# wrapped in a thread so we don't block the event loop.
_DKIM_COMMON_SELECTORS = ("default", "google", "k1", "selector1", "selector2", "s1", "s2")


def _hostname(url: str) -> str:
    try:
        p = urlparse(url if "://" in url else f"https://{url}")
        host = (p.hostname or "").lower()
        # Strip leading www. so DNS queries target the root domain — SPF /
        # DMARC are always on the apex.
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _txt_records(domain: str) -> List[str]:
    """Fetch TXT records for ``domain``. Returns empty list on any error."""
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver()
        # Tightened from 4s → 2s so the DKIM probe (7 selectors) can't
        # blow 28s of budget on slow / non-existent records.
        resolver.lifetime = 2.0
        resolver.timeout = 2.0
        answers = resolver.resolve(domain, "TXT")
        out: List[str] = []
        for r in answers:
            # dnspython wraps each chunk in quotes; rejoin them.
            parts = [s.decode() if isinstance(s, bytes) else str(s) for s in r.strings]
            out.append("".join(parts))
        return out
    except Exception:
        return []


def _mx_records(domain: str) -> List[str]:
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver()
        resolver.lifetime = 2.0
        resolver.timeout = 2.0
        answers = resolver.resolve(domain, "MX")
        return [str(r.exchange).rstrip(".") for r in answers]
    except Exception:
        return []


async def domain_reputation(url: str) -> Dict[str, Any]:
    """Check SPF, DKIM, DMARC, MX for the domain behind ``url``.

    DKIM selectors are vendor-specific, so we only probe the half-dozen
    universal ones (Google's "google", Mailchimp's "k1", Microsoft's
    "selector1/2", etc.). A miss here doesn't mean DKIM is broken — it
    just means we couldn't verify with the common selectors; the audit
    surfaces that nuance honestly.

    All DNS lookups (SPF TXT, DMARC TXT, MX, 7 × DKIM TXT) fire in
    parallel via ``asyncio.to_thread`` so the worst-case wall time is
    the single slowest lookup (~2s) instead of sum-of-all (~18s).
    """
    host = _hostname(url)
    if not host:
        return {"available": False, "error": "no domain"}

    try:
        # All 10 DNS queries fire concurrently. Each one is sync (dnspython
        # has no async API in 2.x), so we offload each to a worker thread.
        results = await asyncio.gather(
            asyncio.to_thread(_txt_records, host),
            asyncio.to_thread(_txt_records, f"_dmarc.{host}"),
            asyncio.to_thread(_mx_records, host),
            asyncio.to_thread(socket.gethostbyname, host),
            *(asyncio.to_thread(_txt_records, f"{sel}._domainkey.{host}")
              for sel in _DKIM_COMMON_SELECTORS),
            return_exceptions=True,
        )
        spf_raw       = results[0] if isinstance(results[0], list) else []
        dmarc_raw     = results[1] if isinstance(results[1], list) else []
        mx            = results[2] if isinstance(results[2], list) else []
        ip            = results[3] if isinstance(results[3], str)  else ""
        dkim_results  = results[4:]

        spf_records = [t for t in spf_raw if t.lower().startswith("v=spf1")]
        dmarc_records = [t for t in dmarc_raw if t.lower().startswith("v=dmarc1")]
        dkim_found: List[str] = []
        for sel, recs in zip(_DKIM_COMMON_SELECTORS, dkim_results):
            if isinstance(recs, list) and any("v=dkim1" in r.lower() for r in recs):
                dkim_found.append(sel)

        result = {
            "domain": host,
            "ip": ip,
            "spf": {
                "present": bool(spf_records),
                "record": spf_records[0] if spf_records else None,
            },
            "dmarc": {
                "present": bool(dmarc_records),
                "record": dmarc_records[0] if dmarc_records else None,
                "policy": _dmarc_policy(dmarc_records[0]) if dmarc_records else None,
            },
            "dkim": {
                "selectors_found": dkim_found,
                "verified": bool(dkim_found),
            },
            "mx": mx[:5],
        }
        # Derive a short "issues" list the audit can quote verbatim.
        issues: List[str] = []
        if not result["spf"]["present"]:
            issues.append("No SPF record — outbound email is trivially spoofable.")
        if not result["dmarc"]["present"]:
            issues.append("No DMARC record — phishing impersonation has no enforcement.")
        elif result["dmarc"]["policy"] == "none":
            issues.append("DMARC policy is `p=none` — monitoring only, no protection from spoofing.")
        if not result["dkim"]["verified"]:
            issues.append("No DKIM signature on common selectors — deliverability and trust scores will suffer.")
        if not result["mx"]:
            issues.append("No MX records — the domain is not configured to receive email.")
        result["issues"] = issues
        result["available"] = True
        return result
    except Exception as exc:
        logger.warning("domain reputation check failed for %s: %s", host, exc)
        return {"available": False, "error": str(exc)[:200]}


def _dmarc_policy(record: str) -> Optional[str]:
    m = re.search(r"\bp\s*=\s*(none|quarantine|reject)\b", record or "", re.I)
    return m.group(1).lower() if m else None
