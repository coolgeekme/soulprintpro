"""app_artifacts.py — interactive HTML/JS app artifacts.

Extends the existing artifact pipeline so an agent can emit a working
mini-application (pricing calculator, ROI tool, quiz, chart dashboard,
landing page snippet, etc.) that renders LIVE in chat AND has:

  • Sandboxed iframe preview inside the chat stream
  • Capability-token public share URL (`/api/share/app/{token}`) that
    serves the HTML with `Content-Type: text/html` so the browser
    renders it instead of downloading
  • Multi-file project support via `[[APP]]…[[/APP]]` JSON manifest
    blocks that get bundled into a downloadable ZIP

Recognition modes:
  1. `[[ARTIFACT name="thing.html" ...]]<html>…</html>[[/ARTIFACT]]`
     → Single self-contained file. The pipeline auto-stamps it as an
     app (kind="app") and mints a share token.

  2. `[[APP name="pricing-calculator" title="…"]]
     {"main":"index.html","files":{"index.html":"…","app.js":"…","styles.css":"…"}}
     [[/APP]]`
     → Multi-file project. Saved as a bundle (one parent doc with
     `is_app_bundle=True` + `app_files: dict[name → content]`),
     plus a real `.zip` file for download.

Public routes:
  GET /api/share/app/{token}           → main HTML, inline
  GET /api/share/app/{token}/{path}    → sibling file from bundle (CSS, JS, etc.)
  GET /api/share/app/{token}/_download → ZIP of the whole bundle

Security:
  • App tokens are 32-hex-char unguessable capability strings
  • Public routes serve files inline but the iframe sandbox attribute
    (set by the frontend) keeps untrusted scripts caged
  • Sibling-file routes validate filename against a safe regex and
    only resolve within the bundle's `app_files` dict — no path
    traversal possible
"""

from __future__ import annotations

import io
import json
import logging
import re
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

logger = logging.getLogger(__name__)

# ── System-prompt directive ─────────────────────────────────────────────────
# Appended to RESOURCEFULNESS_DIRECTIVE by artifacts.py so every agent knows
# how to ship a runnable app when the user asks for one.
APP_ARTIFACT_DIRECTIVE = """

— INTERACTIVE APP ARTIFACTS (new) —
When the user asks for a TOOL the user can run in their browser — a
pricing/ROI calculator, a quiz, a dashboard, a form builder, a chart, a
data viz, a quick widget, an embeddable HTML snippet, a landing-page
prototype — emit a runnable app artifact, NOT a markdown brief about
how to build one.

Two emission patterns:

(1) Single self-contained file (DEFAULT — use this 95% of the time):

    [[ARTIFACT name="pricing-calculator.html" mime="text/html"
               title="Pricing Calculator"]]
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1">
      <title>Pricing Calculator</title>
      <script src="https://cdn.tailwindcss.com"></script>
      <style>/* … */</style>
    </head>
    <body>
      <main class="p-6 max-w-md mx-auto">
        <!-- UI here -->
      </main>
      <script>
        // logic here — vanilla JS or Alpine.js or any CDN-loadable lib
      </script>
    </body>
    </html>
    [[/ARTIFACT]]

(2) Multi-file project (only when the app is genuinely complex enough
    to warrant separate files — usually for GitHub-export use cases):

    [[APP name="lead-scoring-tool" title="Lead Scoring Tool"]]
    {
      "main": "index.html",
      "files": {
        "index.html": "<!DOCTYPE html><html>…</html>",
        "app.js": "// logic …",
        "styles.css": "/* styles … */"
      }
    }
    [[/APP]]

App-artifact rules — NON-NEGOTIABLE:
  • ONE self-contained file by default. No build step, no npm, no React
    JSX without a CDN bundler. The app must work the second the user
    opens the share URL.
  • External libs ONLY via CDN (jsdelivr, unpkg, cdnjs, tailwindcss CDN,
    Google Fonts). Common useful ones: Tailwind, Alpine.js, Chart.js,
    HTMX, D3, Three.js, marked.
  • No data persistence beyond `localStorage` (user's browser only).
  • Mobile-first responsive. Real, usable UI — not a stub.
  • Inline ALL custom CSS and JS in the single-file mode. Don't link
    to local .css / .js files that don't exist.
  • Always include `<meta name="viewport" content="width=device-width">`.
  • Tailwind is preferred for styling (load via the CDN script tag).
  • If the user mentions THEIR business / brand voice / colors, USE the
    org Soul colors / tone if injected; otherwise tasteful neutrals.

Pre-baked deliverables expected:
  ✅ Pricing/ROI/savings/profitability calculator → input fields +
     real-time output + a nice summary card + a "share these inputs"
     URL hash if appropriate.
  ✅ Quiz / scorecard → multi-step questions, score at end, copy CTA.
  ✅ Landing-page section → hero + value props + CTA, ready to embed.
  ✅ Dashboard / chart → demo data + Chart.js or similar.
  ✅ Embeddable widget → fully self-contained <iframe>-friendly markup.

DO NOT use [[APP]] for: briefs, write-ups, plans, reports, copy decks.
Those are .pdf or .docx artifacts.
"""

# ── Block parsing ───────────────────────────────────────────────────────────
APP_BLOCK_RE = re.compile(
    r"\[\[APP\b([^\]]*)\]\](.*?)\[\[/APP\]\]",
    re.DOTALL | re.IGNORECASE,
)
_APP_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
_SAFE_BUNDLE_FILE_RE = re.compile(r"^[a-zA-Z0-9._/\-]{1,80}$")


def extract_app_bundles(text: str) -> list:
    """Pull every `[[APP]]` block out of `text` and return a list of
    parsed bundles. Removes the blocks from the returned-text via the
    caller's own re-strip (we just return the parsed bundles here)."""
    out = []
    for m in APP_BLOCK_RE.finditer(text or ""):
        attrs_raw = m.group(1) or ""
        body_raw = (m.group(2) or "").strip()
        attrs = {k.lower(): v for k, v in _APP_ATTR_RE.findall(attrs_raw)}
        try:
            manifest = json.loads(body_raw)
        except Exception as exc:
            logger.warning("Invalid [[APP]] manifest JSON: %s", exc)
            continue
        if not isinstance(manifest, dict):
            continue
        files = manifest.get("files") or {}
        main = (manifest.get("main") or "index.html").strip()
        if not isinstance(files, dict) or not files:
            continue
        # Sanitise file names
        safe_files = {}
        for name, content in files.items():
            if not isinstance(name, str) or not isinstance(content, str):
                continue
            n = name.strip()
            if not _SAFE_BUNDLE_FILE_RE.match(n):
                continue
            if len(content) > 800_000:  # 800 KB ceiling per file
                content = content[:800_000]
            safe_files[n] = content
        if not safe_files:
            continue
        if main not in safe_files:
            # Pick the first .html as main if the named main is missing.
            html_files = [n for n in safe_files if n.lower().endswith(".html")]
            if html_files:
                main = html_files[0]
            else:
                main = next(iter(safe_files.keys()))
        out.append({
            "name": (attrs.get("name") or "app").strip()[:80],
            "title": (attrs.get("title") or attrs.get("name") or "App").strip()[:160],
            "main": main,
            "files": safe_files,
            "_raw_span": (m.start(), m.end()),
        })
    return out


def strip_app_blocks(text: str) -> str:
    """Remove every `[[APP]]` block from the message text so the
    chat-message body itself doesn't leak the raw JSON."""
    return APP_BLOCK_RE.sub("", text or "").rstrip()


def looks_like_html_app(content: str) -> bool:
    """Heuristic: does `content` look like a self-contained HTML app?"""
    if not content:
        return False
    c = content.lstrip()[:512].lower()
    return c.startswith("<!doctype") or c.startswith("<html")


# ── Persistence helpers ────────────────────────────────────────────────────
def make_app_token() -> str:
    return uuid.uuid4().hex


def build_zip_bytes(files: dict, root_name: str = "app") -> bytes:
    """Bundle a {filename: content} dict into a ZIP archive."""
    buf = io.BytesIO()
    safe_root = re.sub(r"[^a-zA-Z0-9._\-]+", "-", root_name.strip()) or "app"
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            data = content.encode("utf-8", errors="replace") \
                if isinstance(content, str) else bytes(content)
            z.writestr(f"{safe_root}/{name}", data)
        readme = (
            f"# {safe_root}\n\n"
            f"Generated by Foundry. Ready to push to GitHub.\n\n"
            "## Run locally\n"
            "Just open `index.html` in your browser — no build step.\n\n"
            "## Push to GitHub (gh CLI)\n"
            "```bash\n"
            "gh repo create my-repo --public --source=. --remote=origin --push\n"
            "```\n"
        )
        z.writestr(f"{safe_root}/README.md", readme.encode("utf-8"))
    return buf.getvalue()


async def save_app_bundle(
    db,
    *,
    bundle: dict,
    org_id: str,
    conversation_id: str,
    message_id: str,
    agent_id: str,
    agent_name: str,
    created_by: str,
) -> Optional[dict]:
    """Persist a multi-file app bundle and mint a public share token.
    Returns an attachment dict in the chat-message shape, or None on
    failure."""
    try:
        token = make_app_token()
        files = bundle["files"]
        main = bundle["main"]
        title = bundle["title"]
        name = bundle["name"]
        now = datetime.now(timezone.utc)
        main_content = files[main]
        # Build the ZIP once at save time so the download is O(1).
        zip_bytes = build_zip_bytes(files, root_name=name)
        doc = {
            "id": str(uuid.uuid4()),
            "org_id": org_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "created_by": created_by,
            "filename": f"{token}.html",  # virtual; we don't write to disk
            "original_name": f"{name}.html",
            "title": title,
            "mime": "text/html",
            "kind": "app",
            "ext": "html",
            "size": len(main_content.encode("utf-8", errors="replace")),
            "preview": main_content[:600],
            "is_app_bundle": True,
            "app_token": token,
            "app_main": main,
            "app_files": files,
            "app_zip": zip_bytes,
            "app_zip_size": len(zip_bytes),
            "tags": [],
            "created_at": now,
        }
        await db.documents.insert_one({**doc})
        return {
            "filename": f"{token}.html",
            "url": f"/api/share/app/{token}",
            "share_url": f"/api/share/app/{token}",
            "download_url": f"/api/share/app/{token}/_download",
            "mime": "text/html",
            "kind": "app",
            "size": doc["size"],
            "original_name": doc["original_name"],
            "is_artifact": True,
            "is_app_bundle": True,
            "document_id": doc["id"],
            "title": title,
            "app_token": token,
        }
    except Exception as exc:
        logger.exception("save_app_bundle failed: %s", exc)
        return None


async def upgrade_html_to_app(db, doc_id: str) -> Optional[str]:
    """Promote an existing HTML document row to an app artifact by
    minting a share token + flipping kind to 'app'. Returns the token.

    Called after `save_artifacts` for any artifact saved with ext='html'
    that looks like a self-contained app — so single-file HTML
    artifacts also get the live preview + share URL treatment.
    """
    try:
        token = make_app_token()
        await db.documents.update_one(
            {"id": doc_id},
            {"$set": {
                "kind": "app",
                "app_token": token,
                "is_app_bundle": False,
            }},
        )
        return token
    except Exception as exc:
        logger.warning("upgrade_html_to_app failed: %s", exc)
        return None


# ── Public router ──────────────────────────────────────────────────────────
def make_share_router(db, *, docs_root, read_blob_from_gridfs):
    r = APIRouter(prefix="/api")

    async def _serve_main(doc: dict) -> Response:
        # Bundle case → serve the in-doc main file content directly
        if doc.get("is_app_bundle"):
            main = doc.get("app_main") or "index.html"
            content = (doc.get("app_files") or {}).get(main) or ""
            return Response(content=content, media_type="text/html")
        # Single-file case → read bytes from disk or GridFS
        filename = doc.get("filename")
        path = docs_root / doc.get("org_id", "") / filename
        if path.is_file():
            data = path.read_bytes()
            return Response(content=data, media_type="text/html")
        gridfs_id = doc.get("gridfs_id")
        if gridfs_id and read_blob_from_gridfs:
            blob = await read_blob_from_gridfs(db, gridfs_id)
            if blob is not None:
                return Response(content=blob, media_type="text/html")
        raise HTTPException(404, "App file not found")

    @r.get("/share/app/{token}")
    async def serve_app(token: str):
        if not re.match(r"^[a-f0-9]{32}$", token):
            raise HTTPException(400, "Invalid token")
        doc = await db.documents.find_one(
            {"app_token": token, "kind": "app"}
        )
        if not doc:
            raise HTTPException(404, "App not found")
        return await _serve_main(doc)

    @r.get("/share/app/{token}/_download")
    async def download_app_zip(token: str):
        if not re.match(r"^[a-f0-9]{32}$", token):
            raise HTTPException(400, "Invalid token")
        doc = await db.documents.find_one(
            {"app_token": token, "kind": "app"}
        )
        if not doc:
            raise HTTPException(404, "App not found")
        if doc.get("is_app_bundle"):
            zip_bytes = doc.get("app_zip")
        else:
            # Single-file app — build a single-entry ZIP on demand
            res = await _serve_main(doc)
            try:
                main_bytes = res.body
            except Exception:
                main_bytes = b""
            zip_bytes = build_zip_bytes(
                {"index.html": main_bytes.decode("utf-8", errors="replace")
                 if isinstance(main_bytes, bytes) else main_bytes},
                root_name=(doc.get("original_name") or "app").rsplit(".", 1)[0],
            )
        name = (doc.get("original_name") or "app").rsplit(".", 1)[0]
        headers = {
            "Content-Disposition": f'attachment; filename="{name}.zip"'
        }
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers=headers,
        )

    @r.get("/share/app/{token}/{path:path}")
    async def serve_app_sibling(token: str, path: str):
        if not re.match(r"^[a-f0-9]{32}$", token):
            raise HTTPException(400, "Invalid token")
        if not _SAFE_BUNDLE_FILE_RE.match(path or ""):
            raise HTTPException(400, "Invalid path")
        doc = await db.documents.find_one(
            {"app_token": token, "kind": "app", "is_app_bundle": True}
        )
        if not doc:
            raise HTTPException(404, "App not found")
        content = (doc.get("app_files") or {}).get(path)
        if content is None:
            raise HTTPException(404, "File not in bundle")
        # Best-effort content type based on extension
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        mime = {
            "html": "text/html", "htm": "text/html",
            "css": "text/css", "js": "application/javascript",
            "mjs": "application/javascript", "json": "application/json",
            "svg": "image/svg+xml", "txt": "text/plain", "md": "text/markdown",
        }.get(ext, "application/octet-stream")
        return Response(content=content, media_type=mime)

    return r


__all__ = [
    "APP_ARTIFACT_DIRECTIVE",
    "extract_app_bundles",
    "strip_app_blocks",
    "looks_like_html_app",
    "save_app_bundle",
    "upgrade_html_to_app",
    "make_share_router",
    "make_app_token",
    "build_zip_bytes",
]
