"""Resourceful-agent artifacts.

When an agent can't perform an action directly because the underlying
integration isn't built yet (Google Calendar, Slack, GitHub, Notion, Stripe,
email, etc.), it should fall back to producing the *result* of that action as
a downloadable artifact and quietly hand it to the user.

Agents emit artifacts inline using a fenced syntax that's easy for LLMs to
produce reliably and easy for us to parse:

    [[ARTIFACT name="meeting-with-john.ics" mime="text/calendar" title="Meeting with John"]]
    BEGIN:VCALENDAR
    ...
    END:VCALENDAR
    [[/ARTIFACT]]

This module:
  * provides the system-prompt directive injected into every agent
  * extracts + persists artifact blocks from agent replies
  * exposes an Org Documents library router
  * serves artifact files over HTTP (capability-URL pattern, like other media)
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

# ── System prompt directive ────────────────────────────────────────────────
# Injected at the END of every agent's system prompt by build_system_prompt.
# The agents already know who they are and what their org/team's soul is —
# this just teaches the *behavior* of being resourceful and *how* to emit a
# downloadable artifact when they hit a missing-integration wall.
RESOURCEFULNESS_DIRECTIVE = """

— RESOURCEFULNESS PROTOCOL —
You are a resourceful agent. Never refuse a request just because we don't
have a direct integration for the third-party service involved. Instead,
generate the *result* of the action as a downloadable, ready-to-use artifact
and quietly hand it to the user — they can use it immediately and we'll wire
the native integration in later.

— HONESTY ABOUT IDENTIFIERS —
NEVER invent identifiers, URLs, account IDs, API keys, ticket numbers,
conversation IDs, order numbers, or any other reference the system did not
explicitly give you. If a user asks "what's the conversation ID?" or any
similar question, answer honestly: if you can see it in the URL or context,
share it verbatim; if not, say "I don't have that ID exposed — you can find
it in the address bar" and stop. Fabricating an ID like "AIENG-…" or
"TICKET-…" erodes trust and misroutes support requests. Hard rule: if you
haven't seen the exact ID in the prompt or conversation history, you don't
have it.

You DO have access to these tools (already wired in, just describe what you
want and it happens):
  • Image generation (Nano Banana / Gemini) — render any visual the user asks
    for. Just describe it; the system handles it.
  • Video generation (Kie.ai / Veo) — short videos, 60–180s render.
  • Real-time web search (Brave + Perplexity Sonar fallback) — fresh facts
    are injected into your context automatically when relevant.
  • Org-uploaded files — PDFs, docs, txt the org has uploaded are in your
    context already. Cite them by name.
  • Social Queue — the user can one-click queue any of your replies as a
    drafted social post.
  • Schedules — recurring prompts run on a cron / one-shot schedule.
  • Brand assets — logo, colors, tagline, visual style are honored in any
    image generation automatically.

You DO NOT have direct API access to: Google Calendar, Outlook, Gmail/SMTP
sending, Slack, Discord, Telegram, GitHub, Notion, Linear, Jira, Asana,
Trello, Stripe/payments, HubSpot, Salesforce, Twilio/SMS, LinkedIn/X/Meta
publishing, Zoom/Meet, Drive/Dropbox/S3 uploads. For ANY request that would
need one of these, fall back to a self-serve artifact.

Self-serve artifacts you can emit (pick the most useful format for the job):
  • .ics            — calendar invites & meeting blocks
  • .vcf            — contact cards
  • .eml            — RFC-5322 email drafts (importable into Gmail/Outlook)
  • .csv / .tsv     — spreadsheets, exports, lead lists
  • .json           — structured payloads, API request bodies
  • .md             — briefs, plans, checklists, meeting notes, SOPs, posts
  • .txt            — anything plain-text
  • .html           — landing page mockups, email HTML, dashboards
  • .sh / .py / .js — automation scripts the user can run themselves
  • .sql            — query bodies / migrations

To emit an artifact, place a block like this anywhere in your reply (you can
emit multiple per reply if it helps). The user's chat will render the
artifact as a downloadable file attached to your message AND save it to the
Org's Documents library so it's findable later.

[[ARTIFACT name="filename-with-extension" mime="optional/mime" title="Optional human title"]]
…raw file contents go here, exactly as the file should be saved…
[[/ARTIFACT]]

Hard rules for artifact blocks:
  1. The opening tag must be on its own line.
  2. The closing tag [[/ARTIFACT]] must be on its own line.
  3. Filename is required and must end with one of the extensions above.
  4. Content is treated as raw bytes — do NOT wrap it in markdown code fences,
     do NOT escape it, do NOT add commentary inside the block.
  5. Don't mention the existence of the [[ARTIFACT]] block in your prose.
     Just talk naturally ("Here's a calendar invite for that meeting." — the
     UI will surface the file automatically).

Be PROACTIVE: when a structured deliverable would make your reply more
useful (a launch plan, a content calendar, a checklist, a script, a brief),
go ahead and attach it as an artifact in addition to your prose answer.
Don't ask permission first. Default to action.

Stay quiet about missing native integrations — don't apologize, don't say
"coming soon", don't lecture about API limits. Just deliver the artifact.
"""

# ── Block parser ───────────────────────────────────────────────────────────
# Tolerant regex: optional whitespace around tags, dotall over the body, and
# we accept either single or double quotes around attribute values.
_ARTIFACT_RE = re.compile(
    r"\[\[ARTIFACT\s+([^\]]+?)\]\]\s*\n?(.*?)\n?\s*\[\[/ARTIFACT\]\]",
    re.DOTALL | re.IGNORECASE,
)
_ATTR_RE = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")

ALLOWED_EXTS = {
    "ics", "vcf", "eml", "csv", "tsv", "json", "md", "txt",
    "html", "htm", "sh", "py", "js", "ts", "yaml", "yml", "sql",
    "xml", "rtf", "log", "ini", "toml", "env",
}

DEFAULT_MIME = {
    "ics": "text/calendar", "vcf": "text/vcard", "eml": "message/rfc822",
    "csv": "text/csv", "tsv": "text/tab-separated-values",
    "json": "application/json", "md": "text/markdown", "txt": "text/plain",
    "html": "text/html", "htm": "text/html",
    "sh": "application/x-sh", "py": "text/x-python", "js": "application/javascript",
    "ts": "application/typescript", "yaml": "application/x-yaml", "yml": "application/x-yaml",
    "sql": "application/sql", "xml": "application/xml", "rtf": "application/rtf",
    "log": "text/plain", "ini": "text/plain", "toml": "text/plain", "env": "text/plain",
}

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._\-]")


def _safe_filename(raw: str) -> Tuple[str, str]:
    """Sanitise a user-supplied filename. Returns (clean_name, ext_lower)."""
    name = (raw or "").strip().replace("/", "_").replace("\\", "_")
    name = _SAFE_NAME_RE.sub("_", name)
    if not name or name.startswith("."):
        name = f"artifact-{uuid.uuid4().hex[:8]}.txt"
    if "." not in name:
        name += ".txt"
    ext = name.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXTS:
        # Coerce unsupported extensions to .txt so we never lose the content
        name = name.rsplit(".", 1)[0] + ".txt"
        ext = "txt"
    return name[:120], ext


def _parse_attrs(raw: str) -> dict:
    out = {}
    for m in _ATTR_RE.finditer(raw or ""):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def extract_artifact_blocks(text: str) -> Tuple[str, List[dict]]:
    """Parse artifact blocks out of an agent reply.

    Returns `(cleaned_text, blocks)` where each block is::

        {"name": "x.md", "ext": "md", "mime": "text/markdown",
         "title": "...", "content": "raw bytes as utf-8 str"}

    The cleaned_text has the [[ARTIFACT...]]...[[/ARTIFACT]] blocks removed
    and replaced with a thin placeholder so the prose stays readable.
    """
    if not text or "[[ARTIFACT" not in text.upper():
        return text or "", []

    blocks: List[dict] = []

    def _swap(match: re.Match) -> str:
        attrs = _parse_attrs(match.group(1) or "")
        body = match.group(2) or ""
        raw_name = attrs.get("name") or attrs.get("filename") or ""
        if not raw_name:
            return ""  # malformed — drop entirely
        name, ext = _safe_filename(raw_name)
        mime = attrs.get("mime") or DEFAULT_MIME.get(ext, "application/octet-stream")
        title = (attrs.get("title") or "").strip() or name
        blocks.append({
            "name": name, "ext": ext, "mime": mime,
            "title": title, "content": body,
        })
        # Leave nothing in the prose — the attachment row already shows the
        # filename. Per the directive agents shouldn't reference the block.
        return ""

    cleaned = _ARTIFACT_RE.sub(_swap, text)
    # Tidy up double-blank-lines we created
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, blocks


# ── Persistence ────────────────────────────────────────────────────────────
async def save_artifacts(
    db,
    blocks: List[dict],
    *,
    docs_root: Path,
    org_id: str,
    conversation_id: str,
    message_id: str,
    agent_id: str,
    agent_name: str,
    created_by: str,
) -> List[dict]:
    """Persist each block as a file on disk + a `documents` collection record.

    Returns a list of attachment dicts in the same shape used by the rest of
    the chat (`{filename, url, mime, kind, size, original_name}`) so they can
    be appended to the agent message and rendered by ConversationPage.js
    without any frontend changes.
    """
    if not blocks:
        return []
    folder = docs_root / org_id
    folder.mkdir(parents=True, exist_ok=True)

    attachments: List[dict] = []
    now = datetime.now(timezone.utc)

    for b in blocks:
        try:
            content = (b.get("content") or "")
            data = content.encode("utf-8", errors="replace")
            ext = b["ext"]
            disk_name = f"{uuid.uuid4().hex}.{ext}"
            fpath = folder / disk_name
            fpath.write_bytes(data)

            doc = {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "created_by": created_by,
                "filename": disk_name,           # on-disk
                "original_name": b["name"],      # human-friendly
                "title": b.get("title") or b["name"],
                "mime": b["mime"],
                "kind": _kind_for_ext(ext),
                "ext": ext,
                "size": len(data),
                "preview": content[:600],
                "tags": [],
                "created_at": now,
            }
            await db.documents.insert_one({**doc})

            attachments.append({
                "filename": disk_name,
                "url": f"/api/files/docs/{org_id}/{disk_name}",
                "mime": b["mime"],
                "kind": doc["kind"],
                "size": len(data),
                "original_name": b["name"],
                "is_artifact": True,
                "document_id": doc["id"],
            })
        except Exception as exc:  # pragma: no cover — never let one bad
            logger.exception("artifact persistence failed: %s", exc)
            continue
    return attachments


def _kind_for_ext(ext: str) -> str:
    """Map ext → a kind label the frontend already knows how to render."""
    ext = (ext or "").lower()
    if ext in {"md", "txt", "log", "ini", "toml", "env", "rtf"}: return "text"
    if ext in {"csv", "tsv"}: return "data"
    if ext in {"json", "yaml", "yml", "xml"}: return "data"
    if ext in {"html", "htm"}: return "html"
    if ext in {"sh", "py", "js", "ts", "sql"}: return "code"
    if ext in {"ics"}: return "calendar"
    if ext in {"vcf"}: return "contact"
    if ext in {"eml"}: return "email"
    return "file"


# ── HTTP router ────────────────────────────────────────────────────────────
DOC_FILENAME_RE = re.compile(r"^[a-f0-9]{16,}\.[a-z0-9]{1,8}$", re.IGNORECASE)


def make_documents_router(*, db, docs_root: Path,
                          require_org_member, get_current_user):
    """Build the documents router — list/get/delete + capability-URL serving.

    Returns a single `APIRouter` already prefixed with `/api`.
    """
    r = APIRouter(prefix="/api")

    def _serialize(d: dict) -> dict:
        ca = d.get("created_at")
        return {
            "id": d.get("id") or str(d.get("_id")),
            "org_id": d.get("org_id"),
            "conversation_id": d.get("conversation_id"),
            "message_id": d.get("message_id"),
            "agent_id": d.get("agent_id"),
            "agent_name": d.get("agent_name"),
            "created_by": d.get("created_by"),
            "filename": d.get("filename"),
            "original_name": d.get("original_name"),
            "title": d.get("title"),
            "mime": d.get("mime"),
            "kind": d.get("kind"),
            "ext": d.get("ext"),
            "size": d.get("size", 0),
            "preview": d.get("preview", ""),
            "tags": d.get("tags") or [],
            "url": f"/api/files/docs/{d.get('org_id')}/{d.get('filename')}",
            "created_at": ca.isoformat() if isinstance(ca, datetime) else ca,
        }

    @r.get("/orgs/{org_id}/documents")
    async def list_documents(org_id: str, q: str = "", kind: str = "",
                             limit: int = 100,
                             u: dict = Depends(get_current_user)):
        await require_org_member(org_id, u["id"])
        query: dict = {"org_id": org_id}
        if kind:
            query["kind"] = kind
        if q.strip():
            needle = re.escape(q.strip())
            query["$or"] = [
                {"original_name": {"$regex": needle, "$options": "i"}},
                {"title": {"$regex": needle, "$options": "i"}},
                {"preview": {"$regex": needle, "$options": "i"}},
                {"agent_name": {"$regex": needle, "$options": "i"}},
            ]
        cur = db.documents.find(query, {"_id": 0}).sort("created_at", -1).limit(min(limit, 500))
        out = []
        async for d in cur:
            out.append(_serialize(d))
        return out

    @r.delete("/orgs/{org_id}/documents/{doc_id}")
    async def delete_document(org_id: str, doc_id: str,
                              u: dict = Depends(get_current_user)):
        await require_org_member(org_id, u["id"])
        d = await db.documents.find_one({"id": doc_id, "org_id": org_id})
        if not d:
            raise HTTPException(404, "Document not found")
        try:
            (docs_root / org_id / d["filename"]).unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("disk unlink failed for doc %s: %s", doc_id, exc)
        await db.documents.delete_one({"id": doc_id, "org_id": org_id})
        return {"ok": True}

    @r.get("/files/docs/{org_id}/{filename}")
    async def serve_document(org_id: str, filename: str):
        """Capability-URL serving — the random filename is the capability,
        so no Bearer required (mirrors how `/api/files/conv/...` works for
        chat attachments). DB lookup confirms the file is real before
        handing it out, so this isn't a generic file dump."""
        if not DOC_FILENAME_RE.match(filename):
            raise HTTPException(400, "Invalid filename")
        d = await db.documents.find_one({"org_id": org_id, "filename": filename})
        if not d:
            raise HTTPException(404, "Document not found")
        path = docs_root / org_id / filename
        if not path.is_file():
            raise HTTPException(404, "File missing on disk")
        return FileResponse(
            path,
            filename=d.get("original_name") or filename,
            media_type=d.get("mime") or None,
        )

    return r


__all__ = [
    "RESOURCEFULNESS_DIRECTIVE",
    "extract_artifact_blocks",
    "save_artifacts",
    "make_documents_router",
]
