"""Headless browser tool for Mission Mode.

Phase 2 — Layer A (read-only) + Layer B (form interaction).

Powered by Playwright + Chromium. One shared browser instance per process.

Two interaction models, both supported:

  1. **Ephemeral** (Layer A) — one-shot fetch. `browse_url(url, take_screenshot=True)`
     creates a fresh context per call so cookies/cache don't leak between
     missions. Perfect for "read one page, summarise it, move on".

  2. **Sticky mission page** (Layer B) — pass a `mission_id` and each call
     reuses the same context + page, so the LLM can navigate → fill →
     click → submit across multiple tool turns. Cleanup is explicit via
     `cleanup_mission_page(mission_id)`.

New Layer B primitives:

  - `browse_click(mission_id, selector?, text?)`  — click a button or link,
    either by CSS selector or by visible text (case-insensitive substring).
  - `browse_fill(mission_id, fields)`             — fill inputs / textareas
    by label, name, placeholder, or CSS selector. For `<select>` elements
    we pick the option whose visible text matches the requested value.
  - `browse_submit(mission_id, selector?)`        — submit a form; either
    the caller's selector, or the first `button[type=submit]` /
    `input[type=submit]` on the page.

Every Layer B call returns a standard `snapshot` — `{final_url, title,
status, text_preview, screenshot}` — so the Mission loop's tool_result
event has something visual to render.

Safety rails:
  - 30s navigation / action timeout.
  - 50 KB text cap (same as Layer A — keeps LLM working memory cheap).
  - SSRF protection: http(s) only, refuses RFC1918 / loopback / metadata.
  - Form cap: 20 fields per `browse_fill` call, 2000 chars per value.
  - Bot-mitigation: realistic UA, viewport, en-US locale, Accept-Language.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Cap text returned to the agent's working memory — anything larger blows
# past the LLM's context budget and rarely improves the agent's reasoning.
MAX_TEXT_BYTES = 50_000
NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 15_000
MAX_FILL_FIELDS = 20
MAX_FILL_VALUE_LEN = 2000
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

_BROWSER_LOCK = asyncio.Lock()
_browser = None
_playwright = None

# Per-mission sticky contexts. Keyed by mission_id → (context, page, lock).
# A per-mission lock prevents two concurrent tool calls in the same mission
# from stepping on each other (the LLM shouldn't emit concurrent calls but
# the loop uses asyncio.create_task for artifacts so belt + braces).
_MISSION_PAGES: Dict[str, Dict[str, Any]] = {}


# ── SSRF protection ────────────────────────────────────────────────────────
_PRIVATE_HOSTNAMES = {"localhost", "metadata.google.internal", "169.254.169.254"}


def _is_safe_url(raw: str) -> Tuple[bool, str]:
    """Return (ok, error_message_or_normalized_url)."""
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
    """Lazy-init a single shared Chromium. Cheaper than relaunching per call."""
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
        candidates.append("/usr/bin/chromium")
        candidates.append("/usr/bin/chromium-browser")
        candidates.append("/usr/bin/google-chrome")
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
    # Tear down any stragglers first so mission contexts don't leak.
    for mid in list(_MISSION_PAGES.keys()):
        await cleanup_mission_page(mid)
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


# ── Mission-scoped sticky pages (Layer B) ──────────────────────────────────
async def _get_or_create_mission_page(mission_id: str):
    """Return the sticky (context, page, lock) triple for this mission.

    First call per mission launches a fresh Chromium context so cookies /
    form state persist across browse_url → browse_fill → browse_submit
    calls. Subsequent calls reuse the same page.
    """
    entry = _MISSION_PAGES.get(mission_id)
    if entry is not None:
        return entry
    browser = await _ensure_browser()
    ctx = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        ignore_https_errors=False,
    )
    page = await ctx.new_page()
    entry = {"context": ctx, "page": page, "lock": asyncio.Lock()}
    _MISSION_PAGES[mission_id] = entry
    logger.info("Mission %s: new sticky browser page", mission_id)
    return entry


async def cleanup_mission_page(mission_id: str) -> None:
    """Close the sticky context for a mission and drop the bookkeeping."""
    entry = _MISSION_PAGES.pop(mission_id, None)
    if not entry:
        return
    ctx = entry.get("context")
    try:
        if ctx is not None:
            await ctx.close()
    except Exception as exc:
        logger.warning("mission %s context close failed: %s", mission_id, exc)


# ── Public API: Layer A (read-only) ────────────────────────────────────────
async def browse_url(
    url: str,
    *,
    take_screenshot: bool = True,
    upload_dir: Optional[Path] = None,
    mission_id: Optional[str] = None,
) -> dict:
    """Load `url` with Playwright, return rendered text + optional screenshot.

    If `mission_id` is supplied, navigation happens on the mission's sticky
    page so follow-up `browse_fill` / `browse_click` / `browse_submit` calls
    operate on the same loaded page.
    """
    ok, normalized = _is_safe_url(url)
    if not ok:
        return {"error": normalized, "text": "", "title": "", "final_url": "",
                "screenshot": None, "status": 0}

    if mission_id:
        entry = await _get_or_create_mission_page(mission_id)
        async with entry["lock"]:
            return await _drive_navigate(
                entry["page"], normalized,
                take_screenshot=take_screenshot, upload_dir=upload_dir,
                keep_page_open=True,
            )
    # Ephemeral path — existing Layer A behaviour.
    browser = await _ensure_browser()
    ctx = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        ignore_https_errors=False,
    )
    page = await ctx.new_page()
    try:
        return await _drive_navigate(
            page, normalized,
            take_screenshot=take_screenshot, upload_dir=upload_dir,
            keep_page_open=False,
        )
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


async def _drive_navigate(page, url: str, *, take_screenshot: bool,
                          upload_dir: Optional[Path],
                          keep_page_open: bool) -> dict:
    """Navigate `page` to `url` and return the snapshot dict."""
    out: dict = {"final_url": url, "title": "", "text": "",
                 "screenshot": None, "status": 0, "error": ""}
    try:
        resp = await page.goto(url, wait_until="domcontentloaded",
                               timeout=NAV_TIMEOUT_MS)
    except Exception as exc:
        out["error"] = f"Navigation failed: {exc}"
        return out
    out["status"] = resp.status if resp else 0
    out["final_url"] = page.url
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await _populate_snapshot(page, out, take_screenshot, upload_dir)
    return out


async def _populate_snapshot(page, out: dict, take_screenshot: bool,
                             upload_dir: Optional[Path]) -> None:
    """Read title / text / screenshot / page-map off `page` into `out`."""
    try:
        out["title"] = (await page.title()) or ""
    except Exception:
        out["title"] = ""
    try:
        body_text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
    except Exception as exc:
        body_text = ""
        logger.warning("innerText extraction failed: %s", exc)
    out["text"] = _clean_text(body_text)

    # Pull a structured page map so the LLM can see what's clickable / what
    # forms exist. Without this, the agent has to guess at button text from
    # plain innerText, which is exactly the navigation-blindness problem.
    try:
        page_map = await page.evaluate(_PAGE_MAP_JS)
        if isinstance(page_map, dict):
            out["links"] = page_map.get("links") or []
            out["buttons"] = page_map.get("buttons") or []
            out["forms"] = page_map.get("forms") or []
        else:
            out["links"] = out["buttons"] = out["forms"] = []
    except Exception as exc:
        logger.warning("page-map extraction failed: %s", exc)
        out["links"] = out["buttons"] = out["forms"] = []

    if take_screenshot and upload_dir is not None:
        try:
            fname = f"{uuid.uuid4().hex}.png"
            shot_bytes = await page.screenshot(full_page=False, type="png")
            (upload_dir / fname).write_bytes(shot_bytes)
            out["screenshot"] = fname
        except Exception as exc:
            logger.warning("screenshot failed: %s", exc)
            out["screenshot"] = None


# JavaScript payload that extracts a structured "what can I do on this
# page" map. Returned shape:
#   {
#     links:   [{text, href, selector}],
#     buttons: [{text, selector, type}],
#     forms:   [{
#       selector, action, method,
#       fields: [{name, type, label, placeholder, required}],
#       submit_text
#     }],
#   }
# We score links/buttons by "interactive likelihood" (visible, non-empty
# text, ≤ 80 chars, not a footer link) and cap each list to 30 items so
# the agent's context budget stays sane.
_PAGE_MAP_JS = r"""
() => {
  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const cs = window.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
    return true;
  };
  const cssEscape = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : (s || '').replace(/[^a-zA-Z0-9_-]/g, '_');
  const buildSelector = (el) => {
    if (!el) return '';
    if (el.id) return '#' + cssEscape(el.id);
    if (el.name) return el.tagName.toLowerCase() + "[name='" + cssEscape(el.name) + "']";
    // Use a single stable class if available
    const cls = (el.className || '').toString().trim().split(/\s+/).filter(Boolean);
    if (cls.length) return el.tagName.toLowerCase() + '.' + cls.slice(0, 2).map(cssEscape).join('.');
    return el.tagName.toLowerCase();
  };
  const cleanText = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 120);

  // Links
  const linkEls = Array.from(document.querySelectorAll('a[href]'));
  const links = [];
  const seenLinks = new Set();
  for (const a of linkEls) {
    if (links.length >= 30) break;
    if (!isVisible(a)) continue;
    const text = cleanText(a.innerText || a.getAttribute('aria-label') || a.title);
    if (!text || text.length > 80) continue;
    const href = a.href || '';
    const key = text.toLowerCase() + '|' + href;
    if (seenLinks.has(key)) continue;
    seenLinks.add(key);
    links.push({ text, href, selector: buildSelector(a) });
  }

  // Buttons (and role=button + submit inputs)
  const btnEls = Array.from(document.querySelectorAll(
    "button, [role='button'], input[type='submit'], input[type='button']"
  ));
  const buttons = [];
  const seenBtns = new Set();
  for (const b of btnEls) {
    if (buttons.length >= 30) break;
    if (!isVisible(b)) continue;
    const text = cleanText(
      b.innerText || b.value || b.getAttribute('aria-label') || b.title
    );
    if (!text || text.length > 80) continue;
    if (seenBtns.has(text.toLowerCase())) continue;
    seenBtns.add(text.toLowerCase());
    buttons.push({
      text,
      selector: buildSelector(b),
      type: (b.getAttribute('type') || (b.tagName === 'BUTTON' ? 'button' : '')) || ''
    });
  }

  // Forms — with field labels resolved
  const labelFor = (input) => {
    // 1. <label for="id">
    if (input.id) {
      const lbl = document.querySelector("label[for='" + cssEscape(input.id) + "']");
      if (lbl && lbl.innerText) return cleanText(lbl.innerText);
    }
    // 2. wrapping <label>
    let p = input.parentElement;
    while (p && p !== document.body) {
      if (p.tagName === 'LABEL' && p.innerText) return cleanText(p.innerText);
      p = p.parentElement;
    }
    // 3. aria-label / title
    return cleanText(input.getAttribute('aria-label') || input.title || '');
  };

  const formEls = Array.from(document.querySelectorAll('form'));
  const forms = [];
  for (const f of formEls) {
    if (forms.length >= 6) break;
    if (!isVisible(f)) continue;
    const inputs = Array.from(f.querySelectorAll('input, textarea, select'));
    const fields = [];
    for (const inp of inputs) {
      if (!isVisible(inp)) continue;
      const type = (inp.type || inp.tagName.toLowerCase() || '').toLowerCase();
      // Skip hidden / submit / button — those aren't user fields
      if (['hidden', 'submit', 'button', 'image'].includes(type)) continue;
      fields.push({
        name: inp.name || '',
        type,
        label: labelFor(inp),
        placeholder: cleanText(inp.placeholder || ''),
        required: !!inp.required,
      });
      if (fields.length >= 20) break;
    }
    // Submit button text
    let submitText = '';
    const sb = f.querySelector("button[type='submit'], input[type='submit'], button:not([type])");
    if (sb) submitText = cleanText(sb.innerText || sb.value || '');
    forms.push({
      selector: buildSelector(f),
      action: f.getAttribute('action') || '',
      method: (f.getAttribute('method') || 'get').toLowerCase(),
      fields,
      submit_text: submitText,
    });
  }

  return { links, buttons, forms };
}
"""


async def screenshot_url(url: str, upload_dir: Path) -> dict:
    """Visual-only shortcut (ephemeral)."""
    return await browse_url(url, take_screenshot=True, upload_dir=upload_dir)


# ── Public API: Layer B (form interaction) ─────────────────────────────────
async def browse_click(
    mission_id: str,
    *,
    selector: str = "",
    text: str = "",
    upload_dir: Optional[Path] = None,
) -> dict:
    """Click an element on the mission's sticky page.

    Resolution order:
      1. If `selector` is provided, try it as a CSS selector.
      2. Else, if `text` is provided, match the FIRST visible element whose
         inner text contains the substring (case-insensitive) across
         `button`, `a`, `[role=button]`, `input[type=submit]`, `input[type=button]`.

    Returns the standard snapshot plus `{clicked: "<what"}`.
    """
    selector = (selector or "").strip()
    text = (text or "").strip()
    if not selector and not text:
        return {"error": "Provide `selector` (CSS) or `text` (button/link label)."}
    entry = await _get_or_create_mission_page(mission_id)
    async with entry["lock"]:
        page = entry["page"]
        if not page.url or page.url == "about:blank":
            return {"error": "No page is loaded. Call browse_url first."}
        target_desc = ""
        try:
            locator = None
            if selector:
                locator = page.locator(selector).first
                target_desc = f"css={selector!r}"
            else:
                # `get_by_role` is the most robust way to match buttons/links
                # by visible text. We try role=button first, then role=link,
                # role=radio / checkbox (often wrapped in <label>), then
                # submit inputs, then `<label>` elements (which when clicked
                # activate any wrapped form control), and finally a wide
                # has-text fallback.
                candidates = [
                    page.get_by_role("button", name=re.compile(re.escape(text), re.I)).first,
                    page.get_by_role("link", name=re.compile(re.escape(text), re.I)).first,
                    page.get_by_role("radio", name=re.compile(re.escape(text), re.I)).first,
                    page.get_by_role("checkbox", name=re.compile(re.escape(text), re.I)).first,
                    page.locator(
                        f"input[type=submit][value*='{text}' i], "
                        f"input[type=button][value*='{text}' i]"
                    ).first,
                    # <label>Medium</label> wrapping a radio — clicking the
                    # label is how a real user picks the option.
                    page.locator(f"label:has-text('{text}')").first,
                    page.locator(
                        f"button:has-text('{text}'), a:has-text('{text}'), "
                        f"[role=button]:has-text('{text}')"
                    ).first,
                ]
                for cand in candidates:
                    try:
                        if await cand.count() > 0:
                            locator = cand
                            break
                    except Exception:
                        continue
                target_desc = f"text={text!r}"

            if locator is None:
                return {"error": f"No element matched {target_desc}"}
            try:
                await locator.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
            except Exception:
                pass
            try:
                await locator.click(timeout=ACTION_TIMEOUT_MS)
            except Exception as exc:
                return {"error": f"Click failed ({target_desc}): {exc}"}

            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            snap = await _snapshot_mission_page(page, upload_dir)
            snap["clicked"] = target_desc
            return snap
        except Exception as exc:
            return {"error": f"browse_click crash: {exc}"}


async def browse_fill(
    mission_id: str,
    *,
    fields: Dict[str, Any],
    upload_dir: Optional[Path] = None,
) -> dict:
    """Fill form fields on the mission's sticky page.

    `fields` is a dict of `{label_or_selector: value}` pairs. Each key is
    resolved by trying, in order:
      1. As a CSS selector         (`"#email"`, `"input[name=email]"`)
      2. As an ARIA label          (`get_by_label`)
      3. As a placeholder substring
      4. As a `name="..."` attribute

    For `<select>` elements we pick the option whose visible label matches
    `value` (case-insensitive). For checkboxes / radios, truthy values
    check the box, falsy values uncheck it.
    """
    if not isinstance(fields, dict) or not fields:
        return {"error": "`fields` must be a non-empty mapping"}
    if len(fields) > MAX_FILL_FIELDS:
        return {"error": f"Too many fields (max {MAX_FILL_FIELDS})"}

    entry = await _get_or_create_mission_page(mission_id)
    async with entry["lock"]:
        page = entry["page"]
        if not page.url or page.url == "about:blank":
            return {"error": "No page is loaded. Call browse_url first."}

        filled: list = []
        not_found: list = []
        for raw_key, raw_value in fields.items():
            key = str(raw_key or "").strip()
            if not key:
                continue
            if isinstance(raw_value, str) and len(raw_value) > MAX_FILL_VALUE_LEN:
                raw_value = raw_value[:MAX_FILL_VALUE_LEN]
            value = raw_value
            loc, how = await _resolve_form_field(page, key)
            if loc is None:
                not_found.append(key)
                continue
            try:
                tag = (await loc.evaluate("el => el.tagName && el.tagName.toLowerCase()") or "").strip()
                input_type = ""
                if tag == "input":
                    input_type = (await loc.evaluate("el => (el.type || '').toLowerCase()") or "").strip()

                if tag == "select":
                    # Try label match, then value match, then visible text
                    label_val = str(value)
                    try:
                        await loc.select_option(label=label_val, timeout=ACTION_TIMEOUT_MS)
                    except Exception:
                        try:
                            await loc.select_option(value=label_val, timeout=ACTION_TIMEOUT_MS)
                        except Exception:
                            not_found.append(f"{key} (select option not found)")
                            continue
                elif input_type in {"checkbox", "radio"}:
                    want_on = bool(value) and str(value).lower() not in {"false", "no", "off", "0", ""}
                    if want_on:
                        await loc.check(timeout=ACTION_TIMEOUT_MS)
                    else:
                        await loc.uncheck(timeout=ACTION_TIMEOUT_MS)
                elif input_type == "file":
                    not_found.append(f"{key} (file upload not supported yet)")
                    continue
                else:
                    # textarea + text/email/tel/url/password/number/etc.
                    await loc.fill(str(value), timeout=ACTION_TIMEOUT_MS)
                filled.append({"field": key, "matched_by": how})
            except Exception as exc:
                not_found.append(f"{key} ({exc.__class__.__name__})")

        snap = await _snapshot_mission_page(page, upload_dir)
        snap["filled"] = filled
        snap["not_found"] = not_found
        return snap


async def browse_submit(
    mission_id: str,
    *,
    selector: str = "",
    upload_dir: Optional[Path] = None,
) -> dict:
    """Submit a form on the mission's sticky page.

    If `selector` is provided it can point at either a `<form>` (we call
    `form.submit()`) or a button (we click it). If `selector` is empty we
    click the first visible `button[type=submit]` or `input[type=submit]`.
    """
    entry = await _get_or_create_mission_page(mission_id)
    async with entry["lock"]:
        page = entry["page"]
        if not page.url or page.url == "about:blank":
            return {"error": "No page is loaded. Call browse_url first."}
        clicked = ""
        try:
            if selector:
                loc = page.locator(selector).first
                if await loc.count() == 0:
                    return {"error": f"No element matched {selector!r}"}
                tag = (await loc.evaluate("el => el.tagName && el.tagName.toLowerCase()") or "").strip()
                if tag == "form":
                    # Use requestSubmit so native validation + submit events
                    # fire the way the site expects.
                    await loc.evaluate(
                        "el => { if (el.requestSubmit) el.requestSubmit(); else el.submit(); }"
                    )
                    clicked = f"form={selector!r}"
                else:
                    await loc.click(timeout=ACTION_TIMEOUT_MS)
                    clicked = f"css={selector!r}"
            else:
                candidates = [
                    page.locator("button[type=submit]").first,
                    page.locator("input[type=submit]").first,
                    page.locator("button:not([type])").first,  # default type is submit
                ]
                loc = None
                for cand in candidates:
                    try:
                        if await cand.count() > 0:
                            loc = cand
                            break
                    except Exception:
                        continue
                if loc is None:
                    return {"error": "No submit button found on the page"}
                await loc.click(timeout=ACTION_TIMEOUT_MS)
                clicked = "auto=submit-button"
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            snap = await _snapshot_mission_page(page, upload_dir)
            snap["submitted_via"] = clicked
            return snap
        except Exception as exc:
            return {"error": f"browse_submit crash: {exc}"}


async def _resolve_form_field(page, key: str):
    """Locate a form input by CSS selector → ARIA label → placeholder → name.

    Returns (locator_or_None, how_matched_string).
    """
    # 1. As CSS selector (only if it looks like one — don't let "Email"
    #    accidentally match the <em>ail</em> tag via CSS).
    if any(c in key for c in "#.[]:>"):
        try:
            loc = page.locator(key).first
            if await loc.count() > 0:
                return loc, "css"
        except Exception:
            pass

    # 2. ARIA label
    try:
        loc = page.get_by_label(key, exact=False).first
        if await loc.count() > 0:
            return loc, "label"
    except Exception:
        pass

    # 3. Placeholder
    try:
        loc = page.get_by_placeholder(key, exact=False).first
        if await loc.count() > 0:
            return loc, "placeholder"
    except Exception:
        pass

    # 4. name attribute (common for server-rendered forms)
    try:
        loc = page.locator(
            f"input[name='{key}'], textarea[name='{key}'], select[name='{key}']"
        ).first
        if await loc.count() > 0:
            return loc, "name"
    except Exception:
        pass

    # 5. Case-insensitive name fallback (LLM capitalization is inconsistent)
    try:
        ci = key.lower()
        loc = page.locator(
            f"input[name i='{ci}'], textarea[name i='{ci}'], select[name i='{ci}']"
        ).first
        if await loc.count() > 0:
            return loc, "name-ci"
    except Exception:
        pass

    return None, ""


async def _snapshot_mission_page(page, upload_dir: Optional[Path]) -> dict:
    """After a Layer B action, build the standard `{final_url, title, text,
    status, screenshot}` dict. Status isn't available post-click (no
    response object) so we surface 0."""
    out: dict = {"final_url": page.url, "title": "", "text": "",
                 "screenshot": None, "status": 0, "error": ""}
    await _populate_snapshot(page, out, True, upload_dir)
    return out


# ── Text cleanup ───────────────────────────────────────────────────────────
def _clean_text(raw: str) -> str:
    """Collapse whitespace and cap to MAX_TEXT_BYTES."""
    if not raw:
        return ""
    t = re.sub(r"[ \t]+", " ", raw)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = t.replace("\x00", "").strip()
    encoded = t.encode("utf-8")
    if len(encoded) <= MAX_TEXT_BYTES:
        return t
    return encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore") + "\n[…truncated…]"


__all__ = [
    "browse_url", "screenshot_url",
    "browse_click", "browse_fill", "browse_submit",
    "cleanup_mission_page", "shutdown_browser",
]
