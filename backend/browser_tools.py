"""Headless browser tool for Mission Mode (Phase 2 — Layer A: read-only).

Powered by Playwright + Chromium. Single shared browser instance per process,
new context per call so cookies/cache don't leak between missions.

The browser sits behind two coroutines:
  • `browse_url(url, *, take_screenshot=True)` — loads the URL with a real
    Chromium, waits for the network to idle, and returns rendered text, the
    final URL after redirects, the page title, and an optional PNG
    screenshot saved to the uploads dir.
  • `screenshot_url(url)` — visual-only shortcut.

Both are designed to be safe defaults:
  • 30s navigation timeout
  • 50 KB text cap (Mission loop's working memory is small)
  • Common bot-detection mitigations: realistic user-agent, viewport,
    accept-language, en-US locale.
  • SSRF protection: http(s) only, refuses RFC1918 / loopback / metadata IPs

The user-controlled-login flow (Phase 3) will reuse this module by adding
a per-user persistent context dir.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import uuid
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Cap text returned to the agent's working memory — anything larger blows
# past the LLM's context budget and rarely improves the agent's reasoning.
MAX_TEXT_BYTES = 50_000
NAV_TIMEOUT_MS = 30_000
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

_BROWSER_LOCK = asyncio.Lock()
_browser = None
_playwright = None


# ── SSRF protection ────────────────────────────────────────────────────────
_PRIVATE_HOSTNAMES = {"localhost", "metadata.google.internal", "169.254.169.254"}


def _is_safe_url(raw: str) -> Tuple[bool, str]:
    """Return (ok, error_message). Refuses non-http(s) and private IPs."""
    if not raw or not isinstance(raw, str):
        return False, "URL is required"
    raw = raw.strip()
    if not raw:
        return False, "URL is required"
    if "://" not in raw:
        raw = "https://" + raw
    try:
        u = urlparse(raw)
    except Exception:
        return False, "Invalid URL"
    if u.scheme not in {"http", "https"}:
        return False, f"Only http(s) URLs are allowed (got {u.scheme!r})"
    host = (u.hostname or "").lower()
    if not host:
        return False, "URL is missing a host"
    if host in _PRIVATE_HOSTNAMES:
        return False, "Refusing to browse a local/metadata host"
    # Resolve and check IP — block RFC1918 / loopback / link-local
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
                return False, "Refusing to browse a private network address"
    except socket.gaierror:
        return False, f"Couldn't resolve host {host!r}"
    except Exception as exc:
        logger.warning("SSRF resolve check failed for %s: %s", host, exc)
    return True, raw


# ── Browser lifecycle ──────────────────────────────────────────────────────
async def _ensure_browser():
    """Lazy-init a single shared Chromium. Cheaper than re-launching per call."""
    global _browser, _playwright
    if _browser is not None:
        return _browser
    async with _BROWSER_LOCK:
        if _browser is not None:
            return _browser
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        # Find a working Chromium binary. Supervisor's `environment=` clause
        # in this container REPLACES the inherited env so PLAYWRIGHT_BROWSERS
        # _PATH isn't visible at runtime — we have to walk a hardcoded
        # candidate list. Order: env override, system Chromium, bundled
        # Playwright Chromium versions.
        import os as _os
        launch_kwargs = dict(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        candidates = []
        env_exe = _os.environ.get("PLAYWRIGHT_CHROME_EXECUTABLE_PATH")
        if env_exe:
            candidates.append(env_exe)
        # System Chromium shipped in this container
        candidates.append("/usr/bin/chromium")
        candidates.append("/usr/bin/chromium-browser")
        candidates.append("/usr/bin/google-chrome")
        # Playwright-managed bundles in standard locations
        for root in ("/pw-browsers", "/root/.cache/ms-playwright"):
            for sub in ("chromium-1217", "chromium-1208", "chromium-1167",
                        "chromium_headless_shell-1217", "chromium_headless_shell-1208"):
                candidates.append(f"{root}/{sub}/chrome-linux/chrome")
                candidates.append(f"{root}/{sub}/chrome-linux/headless_shell")
        for cand in candidates:
            if cand and Path(cand).is_file():
                launch_kwargs["executable_path"] = cand
                logger.info("Mission browser: using Chromium at %s", cand)
                break
        else:
            logger.warning(
                "Mission browser: no Chromium binary found in %d candidates; "
                "Playwright will fall back to its own resolution.", len(candidates),
            )
        _browser = await _playwright.chromium.launch(**launch_kwargs)
    return _browser


async def shutdown_browser():
    """Called at FastAPI shutdown to release the Chromium process."""
    global _browser, _playwright
    try:
        if _browser is not None:
            await _browser.close()
    except Exception as exc:
        logger.warning("browser close failed: %s", exc)
    finally:
        _browser = None
    try:
        if _playwright is not None:
            await _playwright.stop()
    except Exception as exc:
        logger.warning("playwright stop failed: %s", exc)
    finally:
        _playwright = None


# ── Public API ─────────────────────────────────────────────────────────────
async def browse_url(
    url: str,
    *,
    take_screenshot: bool = True,
    upload_dir: Optional[Path] = None,
) -> dict:
    """Load `url` with Playwright, return rendered text + screenshot path.

    Returns::

        {"final_url": str, "title": str, "text": str,
         "screenshot": "<filename>.png" or None,
         "status": int, "error": str | ""}
    """
    ok, normalized = _is_safe_url(url)
    if not ok:
        return {"error": normalized, "text": "", "title": "", "final_url": "",
                "screenshot": None, "status": 0}

    browser = await _ensure_browser()
    ctx = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        ignore_https_errors=False,
    )
    page = await ctx.new_page()
    out: dict = {"final_url": normalized, "title": "", "text": "",
                 "screenshot": None, "status": 0, "error": ""}
    try:
        try:
            resp = await page.goto(normalized, wait_until="domcontentloaded",
                                   timeout=NAV_TIMEOUT_MS)
        except Exception as exc:
            out["error"] = f"Navigation failed: {exc}"
            return out
        out["status"] = resp.status if resp else 0
        out["final_url"] = page.url
        # Best-effort: wait for network to settle so SPA content has loaded.
        # Don't fail the whole call if it times out — many sites keep
        # connections open forever for analytics / chat widgets.
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        try:
            out["title"] = (await page.title()) or ""
        except Exception:
            out["title"] = ""

        # Pull readable text from the DOM. innerText respects display:none
        # and preserves visible whitespace, which beats raw textContent.
        try:
            body_text = await page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
        except Exception as exc:
            body_text = ""
            logger.warning("innerText extraction failed: %s", exc)
        out["text"] = _clean_text(body_text)

        if take_screenshot and upload_dir is not None:
            try:
                fname = f"{uuid.uuid4().hex}.png"
                shot_bytes = await page.screenshot(full_page=False, type="png")
                (upload_dir / fname).write_bytes(shot_bytes)
                out["screenshot"] = fname
            except Exception as exc:
                logger.warning("screenshot failed: %s", exc)
                out["screenshot"] = None

        return out
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


async def screenshot_url(url: str, upload_dir: Path) -> dict:
    """Visual-only shortcut. Returns `{"screenshot": filename, "final_url": …}`."""
    return await browse_url(url, take_screenshot=True, upload_dir=upload_dir)


# ── Text cleanup ───────────────────────────────────────────────────────────
def _clean_text(raw: str) -> str:
    """Collapse whitespace and cap to MAX_TEXT_BYTES.

    We deliberately don't try to parse the page semantically (no readability,
    no boilerplate removal) at this stage — the LLM is good at finding the
    important parts, and naive heuristics tend to delete the answer the
    user wanted.
    """
    if not raw:
        return ""
    # Collapse runs of whitespace, strip null bytes that occasionally creep
    # in from binary blobs the page evaluated.
    t = re.sub(r"[ \t]+", " ", raw)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = t.replace("\x00", "").strip()
    encoded = t.encode("utf-8")
    if len(encoded) <= MAX_TEXT_BYTES:
        return t
    # Truncate on a UTF-8 boundary so we don't split a multibyte char
    return encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore") + "\n[…truncated…]"


__all__ = ["browse_url", "screenshot_url", "shutdown_browser"]
