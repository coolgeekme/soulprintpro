"""Per-org Notes — text + voice capture, optional agent-context injection.

Design summary (greenlit by user 1d / 2a / 3a / 4c / 5a):
  • Per-note scope picker: `personal` (creator only), `team` (team
    members), `org` (all org members). Default `personal`.
  • `share_with_agents: True` by default — agents in that org can see
    the note when generating responses. Per-note opt-out so the user
    can keep specific notes purely human.
  • Voice notes: Whisper-1 transcription, audio file discarded after
    transcription (cheap, no playback UX to build).
  • UI: dedicated Notes tab + global floating "+ Note" button on every
    org screen.
  • Search: substring match against title + body, sorted by recency.
    Tags supported but no filtering UI in v1.

We mount this as a router factory so server.py can wire the auth
helpers (`get_current_user`, `require_org_member`) without circular
imports.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

logger = logging.getLogger("notes")


_ALLOWED_SCOPES = {"personal", "team", "org"}


# ── Models ───────────────────────────────────────────────────────────


class NoteCreateBody(BaseModel):
    title: str = Field("", max_length=200)
    body: str = Field("", max_length=20000)
    scope: str = Field("personal", max_length=20)
    team_id: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    share_with_agents: bool = True


class NoteUpdateBody(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    body: Optional[str] = Field(None, max_length=20000)
    scope: Optional[str] = Field(None, max_length=20)
    team_id: Optional[str] = None
    tags: Optional[List[str]] = None
    share_with_agents: Optional[bool] = None


# ── Serializer ───────────────────────────────────────────────────────


def serialize_note(n: dict, *, viewer_id: str) -> dict:
    """Strip `_id`, surface `id`, and add `can_edit` (creator-only edit)."""
    return {
        "id": str(n["_id"]),
        "org_id": n.get("org_id", ""),
        "team_id": n.get("team_id"),
        "scope": n.get("scope", "personal"),
        "title": n.get("title", ""),
        "body": n.get("body", ""),
        "tags": n.get("tags", []),
        "share_with_agents": bool(n.get("share_with_agents", True)),
        "source": n.get("source", "text"),  # "text" | "voice"
        "created_at": n.get("created_at"),
        "updated_at": n.get("updated_at"),
        "created_by": n.get("created_by", ""),
        "created_by_name": n.get("created_by_name", ""),
        "can_edit": str(n.get("created_by", "")) == str(viewer_id),
    }


# ── Visibility helpers ───────────────────────────────────────────────


async def _visible_filter(db, org_id: str, user_id: str) -> dict:
    """Build the Mongo filter expressing which notes the viewer can read.

    A user sees:
      • Their own `personal` notes
      • Any `team` notes where they're a member of that team
      • Any `org` notes in the org
    """
    base = {"org_id": org_id}
    team_ids: list[str] = []
    cur = db.team_members.find({"org_id": org_id, "user_id": user_id})
    async for m in cur:
        team_ids.append(m["team_id"])
    return {
        **base,
        "$or": [
            {"scope": "personal", "created_by": user_id},
            {"scope": "team", "team_id": {"$in": team_ids}},
            {"scope": "org"},
        ],
    }


def _normalise_scope(scope: str, team_id: Optional[str]) -> tuple[str, Optional[str]]:
    s = (scope or "personal").lower().strip()
    if s not in _ALLOWED_SCOPES:
        raise HTTPException(422, f"scope must be one of {sorted(_ALLOWED_SCOPES)}")
    if s == "team" and not team_id:
        raise HTTPException(422, "team_id is required when scope='team'")
    if s != "team":
        team_id = None  # ignore stray team_id for personal / org notes
    return s, team_id


# ── Agent context helper (consumed by build_system_prompt) ──────────


async def get_agent_context_notes(
    db, *, org_id: str, viewer_user_id: str,
    team_id: Optional[str] = None, limit: int = 12,
) -> List[dict]:
    """Return the most-recent notes the agent should fold into its
    system prompt context. Filters by:
      • org_id match
      • `share_with_agents: True`
      • the same visibility rules as the human viewer

    Sorted newest-first; capped at `limit` to keep prompt budget tight.
    """
    if not org_id or not viewer_user_id:
        return []
    visible = await _visible_filter(db, org_id, viewer_user_id)
    visible["share_with_agents"] = True
    cur = db.org_notes.find(visible).sort("updated_at", -1).limit(limit)
    out: List[dict] = []
    async for n in cur:
        out.append({
            "id": str(n["_id"]),
            "title": n.get("title", ""),
            "body": n.get("body", ""),
            "tags": n.get("tags", []),
            "scope": n.get("scope", "personal"),
            "updated_at": n.get("updated_at"),
        })
    return out


def format_notes_for_prompt(notes: List[dict]) -> str:
    """Render the agent-context notes block. Empty string when no
    notes — caller can safely concatenate without checks.
    """
    if not notes:
        return ""
    lines = [
        "── ORG NOTES (recent, user-authored) ──",
        "These are notes the user has dropped into this org — captured "
        "thoughts, meeting takeaways, brand musings, reminders. Treat "
        "them as additional context about what's on the user's mind. "
        "Don't quote verbatim unless asked; just let them inform your "
        "answer when relevant.",
        "",
    ]
    for n in notes:
        title = (n.get("title") or "").strip() or "(untitled)"
        body = (n.get("body") or "").strip()
        if len(body) > 1200:
            body = body[:1200].rstrip() + "…"
        scope = n.get("scope", "personal")
        bits = [f"• {title}  ({scope})"]
        if body:
            # Indent the body so it's visually clearly a single note.
            bits.extend(f"    {line}" for line in body.splitlines() if line.strip())
        lines.append("\n".join(bits))
    return "\n\n".join(lines)


# ── Router factory ───────────────────────────────────────────────────


def make_notes_router(*, db, require_org_member, get_current_user):
    r = APIRouter(prefix="/api")

    async def _load_user_name(user_id: str) -> str:
        try:
            u = await db.users.find_one({"_id": ObjectId(user_id)}, {"name": 1, "email": 1})
            return (u or {}).get("name") or (u or {}).get("email") or ""
        except Exception:
            return ""

    @r.get("/orgs/{org_id}/notes")
    async def list_notes(
        org_id: str, q: str = "", limit: int = 100,
        u: dict = Depends(get_current_user),
    ):
        await require_org_member(org_id, u["id"])
        flt = await _visible_filter(db, org_id, u["id"])
        if q.strip():
            esc = re.escape(q.strip())
            flt["$and"] = [{
                "$or": [
                    {"title": {"$regex": esc, "$options": "i"}},
                    {"body":  {"$regex": esc, "$options": "i"}},
                    {"tags":  {"$regex": esc, "$options": "i"}},
                ],
            }]
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        cur = db.org_notes.find(flt).sort("updated_at", -1).limit(limit)
        return [serialize_note(n, viewer_id=u["id"]) async for n in cur]

    @r.post("/orgs/{org_id}/notes", status_code=201)
    async def create_note(
        org_id: str, body: NoteCreateBody,
        u: dict = Depends(get_current_user),
    ):
        await require_org_member(org_id, u["id"])
        scope, team_id = _normalise_scope(body.scope, body.team_id)
        title = (body.title or "").strip()
        body_text = (body.body or "").strip()
        if not title and not body_text:
            raise HTTPException(422, "Note needs at least a title or body.")
        # Derive a title from the first ~60 chars of body when missing.
        if not title:
            first_line = next(
                (ln.strip() for ln in body_text.splitlines() if ln.strip()),
                body_text,
            )
            title = (first_line[:60].rstrip() + ("…" if len(first_line) > 60 else "")) or "Untitled note"
        now = datetime.now(timezone.utc)
        doc = {
            "org_id": org_id,
            "team_id": team_id,
            "scope": scope,
            "title": title,
            "body": body_text,
            "tags": [t.strip() for t in (body.tags or []) if t.strip()][:12],
            "share_with_agents": bool(body.share_with_agents),
            "source": "text",
            "created_by": u["id"],
            "created_by_name": await _load_user_name(u["id"]),
            "created_at": now,
            "updated_at": now,
        }
        res = await db.org_notes.insert_one(doc)
        doc["_id"] = res.inserted_id
        return serialize_note(doc, viewer_id=u["id"])

    @r.post("/orgs/{org_id}/notes/voice", status_code=201)
    async def create_voice_note(
        org_id: str,
        audio: UploadFile = File(...),
        scope: str = Form("personal"),
        team_id: Optional[str] = Form(None),
        title: str = Form(""),
        tags_csv: str = Form(""),
        share_with_agents: bool = Form(True),
        u: dict = Depends(get_current_user),
    ):
        """Upload an audio blob → Whisper → save as a note.

        The audio file itself is discarded after transcription — we
        store the transcript only. (We've shipped the same pattern in
        Telegram inbound; no need for a player UX in v1.)
        """
        await require_org_member(org_id, u["id"])
        scope_n, team_id_n = _normalise_scope(scope, team_id)
        raw = await audio.read()
        if not raw:
            raise HTTPException(422, "Empty audio upload.")
        # Soft size cap — Whisper's hard cap is 25 MB.
        if len(raw) > 24 * 1024 * 1024:
            raise HTTPException(413, "Voice note must be under 24 MB.")

        # Whisper accepts mp3/mp4/mpeg/mpga/m4a/wav/webm/ogg. Browsers
        # mostly send `audio/webm` (Chrome) or `audio/mp4` (Safari).
        ext = "webm"
        ct = (audio.content_type or "").lower()
        if "mp4" in ct or "m4a" in ct or "aac" in ct:
            ext = "m4a"
        elif "wav" in ct:
            ext = "wav"
        elif "mpeg" in ct or "mp3" in ct:
            ext = "mp3"
        elif "ogg" in ct or "oga" in ct or "opus" in ct:
            ext = "ogg"

        try:
            from telegram_inbound import transcribe_voice
            transcript = await transcribe_voice(raw, ext)
        except Exception as exc:
            logger.warning("Voice note transcription failed: %s", exc)
            raise HTTPException(502, "Couldn't transcribe the voice note — try again.")
        transcript = (transcript or "").strip()
        if not transcript:
            raise HTTPException(422, "No speech detected in that recording.")

        title = (title or "").strip()
        if not title:
            first_line = next(
                (ln.strip() for ln in transcript.splitlines() if ln.strip()),
                transcript,
            )
            title = (first_line[:60].rstrip() + ("…" if len(first_line) > 60 else "")) or "Voice note"
        tags = [t.strip() for t in (tags_csv or "").split(",") if t.strip()][:12]
        now = datetime.now(timezone.utc)
        doc = {
            "org_id": org_id,
            "team_id": team_id_n,
            "scope": scope_n,
            "title": title,
            "body": transcript,
            "tags": tags,
            "share_with_agents": bool(share_with_agents),
            "source": "voice",
            "created_by": u["id"],
            "created_by_name": await _load_user_name(u["id"]),
            "created_at": now,
            "updated_at": now,
        }
        res = await db.org_notes.insert_one(doc)
        doc["_id"] = res.inserted_id
        return serialize_note(doc, viewer_id=u["id"])

    @r.get("/orgs/{org_id}/notes/{note_id}")
    async def get_note(
        org_id: str, note_id: str, u: dict = Depends(get_current_user),
    ):
        await require_org_member(org_id, u["id"])
        try:
            n = await db.org_notes.find_one({"_id": ObjectId(note_id), "org_id": org_id})
        except Exception:
            raise HTTPException(400, "Invalid note id")
        if not n:
            raise HTTPException(404, "Note not found")
        # Visibility check
        visible = await _visible_filter(db, org_id, u["id"])
        ok = await db.org_notes.find_one(
            {"_id": ObjectId(note_id), **visible}, {"_id": 1},
        )
        if not ok:
            raise HTTPException(403, "You don't have access to this note.")
        return serialize_note(n, viewer_id=u["id"])

    @r.patch("/orgs/{org_id}/notes/{note_id}")
    async def update_note(
        org_id: str, note_id: str, body: NoteUpdateBody,
        u: dict = Depends(get_current_user),
    ):
        await require_org_member(org_id, u["id"])
        try:
            n = await db.org_notes.find_one({"_id": ObjectId(note_id), "org_id": org_id})
        except Exception:
            raise HTTPException(400, "Invalid note id")
        if not n:
            raise HTTPException(404, "Note not found")
        if str(n.get("created_by")) != str(u["id"]):
            raise HTTPException(403, "Only the note's creator can edit it.")
        update: dict = {}
        if body.title is not None:
            update["title"] = body.title.strip()[:200]
        if body.body is not None:
            update["body"] = body.body[:20000]
        if body.scope is not None:
            scope, team_id_n = _normalise_scope(
                body.scope, body.team_id if body.team_id is not None else n.get("team_id"),
            )
            update["scope"] = scope
            update["team_id"] = team_id_n
        elif body.team_id is not None and (n.get("scope") == "team"):
            update["team_id"] = body.team_id
        if body.tags is not None:
            update["tags"] = [t.strip() for t in body.tags if t.strip()][:12]
        if body.share_with_agents is not None:
            update["share_with_agents"] = bool(body.share_with_agents)
        if not update:
            return serialize_note(n, viewer_id=u["id"])
        update["updated_at"] = datetime.now(timezone.utc)
        await db.org_notes.update_one({"_id": n["_id"]}, {"$set": update})
        fresh = await db.org_notes.find_one({"_id": n["_id"]})
        return serialize_note(fresh, viewer_id=u["id"])

    @r.delete("/orgs/{org_id}/notes/{note_id}", status_code=204)
    async def delete_note(
        org_id: str, note_id: str, u: dict = Depends(get_current_user),
    ):
        await require_org_member(org_id, u["id"])
        try:
            n = await db.org_notes.find_one({"_id": ObjectId(note_id), "org_id": org_id})
        except Exception:
            raise HTTPException(400, "Invalid note id")
        if not n:
            raise HTTPException(404, "Note not found")
        if str(n.get("created_by")) != str(u["id"]):
            raise HTTPException(403, "Only the note's creator can delete it.")
        await db.org_notes.delete_one({"_id": n["_id"]})
        return None

    return r


__all__ = [
    "make_notes_router",
    "get_agent_context_notes",
    "format_notes_for_prompt",
]
