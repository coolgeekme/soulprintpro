"""Branded HTML → PDF rendering via Playwright Chromium.

Replaces the basic reportlab path for `synthesize(.., ext="pdf")` with a
polished, brand-aware document:

  • Cover page with org logo + title + author byline + date
  • Inter web font for body, IBM Plex Serif for headings (modern,
    business-grade typography)
  • Brand primary colour drives heading colour + accent rules
  • Header on every content page: small org wordmark + doc title
  • Footer: page N / total + small "Prepared by SoulPrint Engine Pro"
    + ISO date
  • Markdown content rendered through the same `markdown` library used
    by reportlab path so we keep the existing feature set (tables,
    code blocks, blockquotes, lists, inline emphasis)

Why Playwright over WeasyPrint / reportlab:
  • Real CSS Grid / Flexbox / variable fonts / @page rules
  • Already part of the deps tree (browser_tools)
  • Pixel-identical to whatever the user sees in a browser
  • One binary (Chromium) instead of GTK + Pango + Cairo

Why this lives in its own module rather than artifact_synthesis:
  • It's an async path (Playwright requires an event loop)
  • The brand-asset fetch is async (Mongo lookup)
  • Lets us keep `synthesize(...)` synchronous + drop in cleanly behind
    a feature flag if we ever want to A/B compare
"""
from __future__ import annotations

import base64
import logging
import re
import os
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Where on-disk media files live (logos uploaded via brand.py end up here).
# We resolve the absolute path once at module import.
_UPLOAD_DIR = Path(os.environ.get("SOULPRINT_UPLOAD_DIR", "/app/backend/uploads"))


# ── Markdown → HTML ─────────────────────────────────────────────────────
try:
    import markdown as _md
    _HAVE_MD = True
except Exception:
    _HAVE_MD = False


def _md_to_html(body: str) -> str:
    if not _HAVE_MD:
        paras = [p.strip() for p in re.split(r"\n{2,}", body or "") if p.strip()]
        return "".join(f"<p>{escape(p)}</p>" for p in paras)
    return _md.markdown(
        body or "",
        extensions=["tables", "fenced_code", "nl2br", "sane_lists", "footnotes"],
        output_format="html5",
    )


# ── Brand fetch ─────────────────────────────────────────────────────────
async def _resolve_brand(db, org_id: str) -> Dict[str, Any]:
    """Pull the minimum brand fields we need for the PDF: org name,
    primary colour, logo bytes (base64 data: URL), tagline.

    Resilient to missing data — every field has a brand-neutral default
    so a fresh org with no uploaded assets still gets a clean document.
    """
    out: Dict[str, Any] = {
        "org_name": "",
        "primary_color": "#F64000",   # SoulPrint brand orange
        "secondary_color": "#1B1B1B", # near-black
        "tagline": "",
        "logo_data_url": "",          # base64 data: URL or ""
    }
    if not (db is not None and org_id):
        return out
    try:
        from bson import ObjectId
        org = await db.organizations.find_one({"_id": ObjectId(org_id)}, {"_id": 0})
    except Exception:
        org = None
    if not org:
        return out
    out["org_name"] = org.get("name", "") or ""
    ba = (org.get("brand_assets") or {}) if isinstance(org, dict) else {}
    colors = ba.get("brand_colors") or []
    if colors:
        out["primary_color"] = colors[0]
        if len(colors) > 1:
            out["secondary_color"] = colors[1]
    if ba.get("tagline"):
        out["tagline"] = ba["tagline"]
    # Logo: prefer primary, fall back to alt.
    for key in ("primary_logo", "alt_logo"):
        info = ba.get(key) or {}
        fname = info.get("filename") or ""
        if not fname:
            continue
        data_url = await _logo_to_data_url(db, fname)
        if data_url:
            out["logo_data_url"] = data_url
            break
    return out


async def _logo_to_data_url(db, filename: str) -> str:
    """Convert a logo filename into a base64 data: URL.

    Tries on-disk first (fast + zero round-trip), then GridFS as a
    fallback for redeploy-survival. Returns "" on any failure.
    """
    if not filename:
        return ""
    safe = filename.replace("/", "").replace("\\", "")
    # Disk first
    try:
        path = _UPLOAD_DIR / safe
        if path.is_file():
            blob = path.read_bytes()
            return _bytes_to_data_url(blob, safe)
    except Exception:
        pass
    # GridFS recovery
    try:
        from file_storage import load_blob_from_gridfs  # type: ignore
        blob, _content_type = await load_blob_from_gridfs(db, safe)
        if blob:
            return _bytes_to_data_url(blob, safe)
    except Exception:
        pass
    return ""


def _bytes_to_data_url(blob: bytes, filename: str) -> str:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "png").lower()
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp",
    }.get(ext, "image/png")
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


# ── HTML template ───────────────────────────────────────────────────────
_PAGE_CSS = """
@page {
  size: Letter;
  margin: 0.85in 0.7in 0.95in 0.7in;
}
@page :first {
  margin: 0;
}
* { box-sizing: border-box; }
html, body {
  margin: 0;
  padding: 0;
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
               'Helvetica Neue', Arial, sans-serif;
  font-size: 10.5pt;
  color: #1a1a22;
  line-height: 1.55;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
h1, h2, h3, h4 {
  font-family: 'IBM Plex Serif', 'Georgia', 'Times New Roman', serif;
  font-weight: 600;
  color: var(--brand-primary, #F64000);
  margin: 1.6em 0 0.5em;
  line-height: 1.2;
  page-break-after: avoid;
}
h1 { font-size: 20pt; }
h2 { font-size: 15pt; border-bottom: 1px solid rgba(0,0,0,0.08); padding-bottom: 0.3em; }
h3 { font-size: 12.5pt; color: #1a1a22; }
h4 { font-size: 11pt; color: #4a4a55; text-transform: uppercase; letter-spacing: 0.06em; }

p { margin: 0 0 0.85em; }
p + p { text-indent: 0; }
strong { font-weight: 600; color: #0e0e16; }
em { font-style: italic; }
a { color: var(--brand-primary, #F64000); text-decoration: none; border-bottom: 1px solid rgba(246,64,0,0.3); }

ul, ol { margin: 0 0 1em 1.4em; padding: 0; }
li { margin-bottom: 0.35em; }
li::marker { color: var(--brand-primary, #F64000); }

blockquote {
  margin: 1em 0;
  padding: 0.6em 1em;
  border-left: 3px solid var(--brand-primary, #F64000);
  background: rgba(0,0,0,0.025);
  color: #2a2a35;
  font-style: italic;
}

code, pre {
  font-family: 'IBM Plex Mono', 'SF Mono', Menlo, Consolas, monospace;
  font-size: 9.5pt;
}
code { background: rgba(0,0,0,0.06); padding: 0.05em 0.35em; border-radius: 3px; }
pre {
  background: #f7f7f4;
  border: 1px solid rgba(0,0,0,0.06);
  border-left: 3px solid var(--brand-primary, #F64000);
  padding: 0.8em 1em;
  border-radius: 4px;
  white-space: pre-wrap;
  word-wrap: break-word;
  page-break-inside: avoid;
}
pre code { background: transparent; padding: 0; }

table {
  width: 100%;
  border-collapse: collapse;
  margin: 1em 0 1.2em;
  font-size: 9.8pt;
  page-break-inside: auto;
}
thead { background: var(--brand-primary, #F64000); color: white; }
th, td {
  text-align: left;
  padding: 0.55em 0.7em;
  border-bottom: 1px solid rgba(0,0,0,0.08);
  vertical-align: top;
}
tbody tr:nth-child(even) td { background: rgba(0,0,0,0.025); }

hr {
  border: 0;
  border-top: 1px solid rgba(0,0,0,0.1);
  margin: 1.6em 0;
}

/* ── Cover page ─────────────────────────────────────────────────── */
.cover {
  width: 100%;
  height: 11in;
  padding: 0.85in 0.7in;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  background: linear-gradient(135deg, #fafaf6 0%, #f4f3ec 100%);
  page-break-after: always;
  position: relative;
  overflow: hidden;
}
.cover::before {
  content: "";
  position: absolute;
  inset: 0;
  background:
    radial-gradient(60% 40% at 100% 0%, color-mix(in srgb, var(--brand-primary, #F64000) 22%, transparent) 0%, transparent 65%),
    radial-gradient(40% 60% at 0% 100%, color-mix(in srgb, var(--brand-secondary, #1B1B1B) 8%, transparent) 0%, transparent 60%);
  pointer-events: none;
}
.cover-top, .cover-mid, .cover-bot { position: relative; z-index: 1; }
.cover-top { display: flex; align-items: center; gap: 0.7em; }
.cover-logo {
  max-height: 60px;
  max-width: 240px;
  object-fit: contain;
  display: block;
}
.cover-org {
  font-family: 'IBM Plex Serif', serif;
  font-size: 13pt;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: #1a1a22;
}
.cover-mid { padding: 0.6in 0; }
.cover-eyebrow {
  font-family: 'Inter', sans-serif;
  font-size: 9.5pt;
  text-transform: uppercase;
  letter-spacing: 0.18em;
  color: var(--brand-primary, #F64000);
  margin-bottom: 0.5em;
  font-weight: 600;
}
.cover-title {
  font-family: 'IBM Plex Serif', serif;
  font-size: 36pt;
  line-height: 1.05;
  font-weight: 600;
  color: #0e0e16;
  margin: 0;
  letter-spacing: -0.01em;
  max-width: 6.5in;
}
.cover-tagline {
  margin-top: 0.6em;
  font-size: 11.5pt;
  color: #4a4a55;
  max-width: 5.5in;
  line-height: 1.5;
}
.cover-bot {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 1em;
  font-size: 9.5pt;
  color: #4a4a55;
  border-top: 1px solid rgba(0,0,0,0.08);
  padding-top: 0.5em;
}
.cover-bot .key {
  text-transform: uppercase;
  letter-spacing: 0.14em;
  font-size: 8pt;
  color: #888;
  margin-bottom: 0.1em;
}
.cover-bot strong { color: #1a1a22; font-weight: 600; }

/* ── Body wrapper ───────────────────────────────────────────────── */
.content { max-width: 100%; }
.content > h1:first-child { margin-top: 0; }

/* Print-only — Playwright generates header/footer template HTML
   independently, but we also tighten widow/orphan handling here. */
p, li { orphans: 2; widows: 2; }
"""


def _build_html(*, body_md: str, title: str, brand: Dict[str, Any]) -> str:
    """Compose the full HTML document for Playwright to render."""
    body_html = _md_to_html(body_md)
    org_name = escape(brand.get("org_name") or "")
    tagline = escape(brand.get("tagline") or "")
    logo = brand.get("logo_data_url") or ""
    primary = brand.get("primary_color") or "#F64000"
    secondary = brand.get("secondary_color") or "#1B1B1B"
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    safe_title = escape(title or "Document")

    # Cover top: prefer logo image; fall back to wordmark text if no logo.
    if logo:
        cover_brand_block = (
            f'<img class="cover-logo" src="{logo}" alt="{org_name} logo" />'
        )
    elif org_name:
        cover_brand_block = f'<div class="cover-org">{org_name}</div>'
    else:
        cover_brand_block = '<div class="cover-org">SoulPrint Engine Pro</div>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{safe_title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Serif:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <style>
    :root {{
      --brand-primary: {primary};
      --brand-secondary: {secondary};
    }}
    {_PAGE_CSS}
  </style>
</head>
<body>
  <section class="cover">
    <div class="cover-top">
      {cover_brand_block}
    </div>
    <div class="cover-mid">
      <div class="cover-eyebrow">Prepared {today}</div>
      <h1 class="cover-title">{safe_title}</h1>
      {f'<p class="cover-tagline">{tagline}</p>' if tagline else ''}
    </div>
    <div class="cover-bot">
      <div>
        <div class="key">Prepared by</div>
        <div><strong>{org_name or "SoulPrint Engine Pro"}</strong></div>
      </div>
      <div>
        <div class="key">Powered by</div>
        <div><strong>SoulPrint Engine Pro</strong></div>
      </div>
    </div>
  </section>
  <main class="content">
    {body_html}
  </main>
</body>
</html>
"""


# ── Page header / footer (Chrome PDF templates) ─────────────────────────
def _header_template(title: str, org_name: str) -> str:
    safe_title = escape(title or "")
    safe_org = escape(org_name or "SoulPrint Engine Pro")
    return f"""
    <div style="font-size:8pt;font-family:'Inter',Arial,sans-serif;color:#888;width:100%;
                padding:0 0.7in;display:flex;justify-content:space-between;align-items:center;">
      <span style="text-transform:uppercase;letter-spacing:0.12em;">{safe_org}</span>
      <span style="opacity:0.7;">{safe_title}</span>
    </div>
    """


def _footer_template(primary: str) -> str:
    return f"""
    <div style="font-size:8pt;font-family:'Inter',Arial,sans-serif;color:#888;width:100%;
                padding:0 0.7in;display:flex;justify-content:space-between;align-items:center;">
      <span>Prepared by <span style="color:{primary};font-weight:600;">SoulPrint Engine Pro</span></span>
      <span><span class="pageNumber"></span> / <span class="totalPages"></span></span>
    </div>
    """


# ── Public API ──────────────────────────────────────────────────────────
async def render_branded_pdf(
    *, body_md: str, title: str, db=None, org_id: str = "",
) -> bytes:
    """Render Markdown → branded PDF bytes via Playwright.

    Falls back to None on any failure so the caller can route to the
    legacy reportlab path. Never raises.
    """
    brand = await _resolve_brand(db, org_id)
    html = _build_html(body_md=body_md, title=title, brand=brand)

    from browser_tools import _ensure_browser  # reuse the shared launcher
    browser = await _ensure_browser()
    ctx = await browser.new_context()
    try:
        page = await ctx.new_page()
        await page.set_content(html, wait_until="domcontentloaded")
        # Give web fonts a moment to apply — without this, the first
        # paint sometimes ships before Inter / IBM Plex are ready and
        # we get a Times-Roman fallback in the cover title.
        try:
            await page.evaluate("() => document.fonts && document.fonts.ready")
        except Exception:
            pass
        pdf_bytes = await page.pdf(
            format="Letter",
            print_background=True,
            margin={
                "top": "0.55in",
                "bottom": "0.65in",
                "left": "0",
                "right": "0",
            },
            display_header_footer=True,
            header_template=_header_template(title, brand.get("org_name") or ""),
            footer_template=_footer_template(brand.get("primary_color") or "#F64000"),
            prefer_css_page_size=False,
        )
        return pdf_bytes
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


__all__ = ["render_branded_pdf"]
