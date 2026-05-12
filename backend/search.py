"""search.py — unified org-wide search across messages, conversations,
missions, documents, social posts, and agents.

Two endpoints:
  GET  /api/search?org_id=...&q=...&limit=...
       Returns grouped, ranked results across every searchable resource
       in the org. Authoritative source of truth — fast, transparent,
       no LLM in the path.

  POST /api/search/synthesize
       Body: { org_id, q, top: [hits] }
       Runs the Emergent LLM over the hit set to produce a synthesized
       answer with inline citations. Powers the "Ask Foundry" button.

Ranking: case-insensitive regex match + recency boost. We compute a
simple score (text-hit count + freshness) per row and sort.

Scoping: every query is gated by membership in the target org. Custom
agents are filtered by `org_scope`; built-in agents are universal.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


_db = None
_AGENTS = None
_get_current_user = None
_require_org_member = None
_llm_chat = None
_user_message = None
_emergent_key = None


def init(*, db, AGENTS, get_current_user, require_org_member,
         LlmChat, UserMessage, EMERGENT_LLM_KEY):
    global _db, _AGENTS, _get_current_user, _require_org_member
    global _llm_chat, _user_message, _emergent_key
    _db = db
    _AGENTS = AGENTS
    _get_current_user = get_current_user
    _require_org_member = require_org_member
    _llm_chat = LlmChat
    _user_message = UserMessage
    _emergent_key = EMERGENT_LLM_KEY


def _safe_regex(q: str) -> re.Pattern:
    return re.compile(re.escape(q.strip()), re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts_or_none(v):
    """Return a datetime if v is one, else None. Accepts ISO strings."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _recency_boost(ts) -> float:
    """0..1 — newer hits score higher."""
    t = _ts_or_none(ts)
    if not t:
        return 0.0
    age_days = max(0.0, (_now() - t).total_seconds() / 86400.0)
    if age_days < 1:
        return 1.0
    if age_days < 7:
        return 0.7
    if age_days < 30:
        return 0.4
    if age_days < 90:
        return 0.2
    return 0.05


def _excerpt(text: str, q: str, ctx: int = 80) -> str:
    """Return a snippet around the first match of q (case-insensitive)."""
    if not text:
        return ""
    t = text
    idx = t.lower().find(q.lower())
    if idx < 0:
        return t[:160] + ("…" if len(t) > 160 else "")
    a = max(0, idx - ctx)
    b = min(len(t), idx + len(q) + ctx)
    out = ("…" if a > 0 else "") + t[a:b] + ("…" if b < len(t) else "")
    return out


async def _team_ids_for_org(org_id: str) -> list:
    cur = _db.teams.find({"org_id": org_id}, {"_id": 1})
    return [str(t["_id"]) async for t in cur]


async def _search_conversations(org_id: str, rx: re.Pattern, limit: int):
    team_ids = await _team_ids_for_org(org_id)
    if not team_ids:
        return []
    docs = await _db.conversations.find(
        {"team_id": {"$in": team_ids}, "title": rx},
        {"_id": 1, "title": 1, "team_id": 1, "type": 1, "created_at": 1},
    ).limit(limit * 2).to_list(limit * 2)
    out = []
    for d in docs:
        out.append({
            "kind": "conversation",
            "id": str(d["_id"]),
            "title": d.get("title") or "(untitled)",
            "team_id": str(d.get("team_id", "")),
            "type": d.get("type"),
            "excerpt": d.get("title") or "",
            "ts": d.get("created_at"),
            "url": f"/app/team/{d.get('team_id','')}/c/{d['_id']}",
        })
    return out


async def _search_messages(org_id: str, q: str, rx: re.Pattern, limit: int):
    team_ids = await _team_ids_for_org(org_id)
    if not team_ids:
        return []
    convs = await _db.conversations.find(
        {"team_id": {"$in": team_ids}}, {"_id": 1, "team_id": 1, "title": 1},
    ).to_list(2000)
    if not convs:
        return []
    conv_ids = [str(c["_id"]) for c in convs]
    conv_meta = {str(c["_id"]): c for c in convs}
    docs = await _db.messages.find(
        {"conversation_id": {"$in": conv_ids}, "text": rx},
        {"_id": 0, "id": 1, "conversation_id": 1, "text": 1, "sender_name": 1,
         "sender_type": 1, "timestamp": 1},
    ).sort("timestamp", -1).limit(limit * 3).to_list(limit * 3)
    out = []
    for m in docs:
        cid = m.get("conversation_id", "")
        cmeta = conv_meta.get(cid) or {}
        out.append({
            "kind": "message",
            "id": m.get("id") or "",
            "title": m.get("sender_name") or "Message",
            "excerpt": _excerpt(m.get("text", ""), q),
            "conversation_id": cid,
            "conversation_title": cmeta.get("title") or "(untitled)",
            "team_id": str(cmeta.get("team_id", "")),
            "ts": m.get("timestamp"),
            "url": f"/app/team/{cmeta.get('team_id','')}/c/{cid}",
        })
    return out


async def _search_missions(org_id: str, q: str, rx: re.Pattern, limit: int):
    docs = await _db.missions.find(
        {"org_id": org_id, "$or": [{"goal": rx}, {"summary": rx}]},
        {"_id": 0, "id": 1, "goal": 1, "summary": 1, "status": 1,
         "lead_agent_name": 1, "started_at": 1, "finished_at": 1},
    ).sort("started_at", -1).limit(limit * 2).to_list(limit * 2)
    out = []
    for m in docs:
        body = (m.get("summary") or m.get("goal") or "")
        out.append({
            "kind": "mission",
            "id": m.get("id") or "",
            "title": (m.get("goal") or "")[:140] or "Mission",
            "excerpt": _excerpt(body, q),
            "status": m.get("status"),
            "lead_agent": m.get("lead_agent_name"),
            "ts": m.get("finished_at") or m.get("started_at"),
            "url": f"/app/missions/{m.get('id','')}",
        })
    return out


async def _search_documents(org_id: str, q: str, rx: re.Pattern, limit: int):
    docs = await _db.documents.find(
        {"org_id": org_id,
         "$or": [{"original_name": rx}, {"preview": rx}, {"filename": rx}]},
        {"_id": 0, "id": 1, "filename": 1, "original_name": 1, "kind": 1,
         "mime": 1, "preview": 1, "size": 1, "created_at": 1, "agent_name": 1},
    ).sort("created_at", -1).limit(limit * 2).to_list(limit * 2)
    out = []
    for d in docs:
        name = d.get("original_name") or d.get("filename") or "Document"
        out.append({
            "kind": "document",
            "id": d.get("id") or "",
            "title": name,
            "excerpt": _excerpt(d.get("preview") or name, q),
            "doc_kind": d.get("kind"),
            "mime": d.get("mime"),
            "size": d.get("size"),
            "by": d.get("agent_name"),
            "ts": d.get("created_at"),
            "url": f"/api/files/docs/{org_id}/{d.get('filename','')}",
        })
    return out


async def _search_social_posts(org_id: str, q: str, rx: re.Pattern, limit: int):
    docs = await _db.social_posts.find(
        {"org_id": org_id, "text": rx},
        {"_id": 0, "id": 1, "text": 1, "platforms": 1, "status": 1,
         "scheduled_for": 1, "published_at": 1, "created_at": 1, "agent_name": 1},
    ).sort("created_at", -1).limit(limit * 2).to_list(limit * 2)
    out = []
    for s in docs:
        text = s.get("text") or ""
        out.append({
            "kind": "social_post",
            "id": s.get("id") or "",
            "title": text[:80] or "Social post",
            "excerpt": _excerpt(text, q),
            "platforms": s.get("platforms", []),
            "status": s.get("status"),
            "by": s.get("agent_name"),
            "ts": (s.get("published_at") or s.get("scheduled_for")
                   or s.get("created_at")),
            "url": f"/app/org/{org_id}/social",
        })
    return out


def _search_agents(org_id: str, q: str, rx: re.Pattern, limit: int):
    """In-memory match across built-ins + this org's custom agents."""
    q_lower = q.lower()
    out = []
    for a in (_AGENTS or {}).values():
        if a.get("custom") and a.get("org_scope") != org_id:
            continue
        haystack = (
            (a.get("name", "") or "") + " " +
            (a.get("role", "") or "") + " " +
            (a.get("description", "") or "") + " " +
            (a.get("division", "") or "")
        ).lower()
        if q_lower not in haystack:
            continue
        out.append({
            "kind": "agent",
            "id": a.get("id"),
            "title": a.get("name") or a.get("id"),
            "excerpt": a.get("description") or a.get("role") or "",
            "division": a.get("division"),
            "emoji": a.get("emoji"),
            "is_custom": bool(a.get("custom")),
            "ts": None,  # agents have no recency
            "url": None,  # opened via team picker, not deep-linked
        })
    return out[:limit * 2]


def _score(hit: dict, q: str) -> float:
    """Combine text-density + recency."""
    q_lower = q.lower()
    txt = (
        (hit.get("title") or "") + " " + (hit.get("excerpt") or "")
    ).lower()
    matches = txt.count(q_lower)
    rec = _recency_boost(hit.get("ts"))
    # Title-hit bonus
    title_bonus = 1.0 if q_lower in (hit.get("title") or "").lower() else 0.0
    return matches * 1.0 + rec * 1.2 + title_bonus * 0.8


def _sanitize_hit(h: dict) -> dict:
    """Strip non-JSON-serialisable values for the wire."""
    out = {}
    for k, v in h.items():
        if isinstance(v, datetime):
            out[k] = v.astimezone(timezone.utc).isoformat() if v.tzinfo else v.replace(tzinfo=timezone.utc).isoformat()
        elif isinstance(v, ObjectId):
            out[k] = str(v)
        else:
            out[k] = v
    return out


# ── Router ──────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/search")


async def _user_dep_callable():
    """Late-bound auth dependency. Server.py wires _get_current_user
    at init() time; we delegate to it here."""
    raise HTTPException(500, "should be replaced by FastAPI at runtime")


from fastapi import Request as _Request  # noqa: E402


async def _resolve_user(request: _Request) -> dict:
    if _get_current_user is None:
        raise HTTPException(500, "search not initialised")
    return await _get_current_user(request)


@router.get("")
async def search(
    org_id: str = Query(..., description="Org to scope the search to"),
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(8, ge=1, le=30),
    user: dict = Depends(_resolve_user),
):
    """Unified org-wide search. Returns hits grouped by kind."""
    await _require_org_member(org_id, user["id"])
    return await _search_impl(org_id=org_id, q=q, limit=limit)


async def _search_impl(*, org_id: str, q: str, limit: int):
    # Resolve user manually since this function is the body of a Depends
    # chain; FastAPI is already calling _user_dep through the route below.
    # (We piggy-back the route declared with Depends for auth.)
    rx = _safe_regex(q)

    convs = await _search_conversations(org_id, rx, limit)
    msgs = await _search_messages(org_id, q, rx, limit)
    missions = await _search_missions(org_id, q, rx, limit)
    docs = await _search_documents(org_id, q, rx, limit)
    posts = await _search_social_posts(org_id, q, rx, limit)
    agents = _search_agents(org_id, q, rx, limit)

    grouped = {
        "agents": sorted(agents, key=lambda h: -_score(h, q))[:limit],
        "conversations": sorted(convs, key=lambda h: -_score(h, q))[:limit],
        "messages": sorted(msgs, key=lambda h: -_score(h, q))[:limit],
        "missions": sorted(missions, key=lambda h: -_score(h, q))[:limit],
        "documents": sorted(docs, key=lambda h: -_score(h, q))[:limit],
        "social_posts": sorted(posts, key=lambda h: -_score(h, q))[:limit],
    }

    top = []
    for arr in grouped.values():
        top.extend(arr)
    top.sort(key=lambda h: -_score(h, q))
    top = top[:limit]

    return {
        "q": q,
        "org_id": org_id,
        "counts": {k: len(v) for k, v in grouped.items()},
        "top": [_sanitize_hit(h) for h in top],
        "groups": {k: [_sanitize_hit(h) for h in v] for k, v in grouped.items()},
    }


# Re-declare the route with proper auth gating now that we have the deps.
@router.get("/v1")
async def search_v1(
    org_id: str = Query(...),
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(8, ge=1, le=30),
    user: dict = Depends(_resolve_user),
):
    """Alias kept for FE forward-compat. Same impl as /api/search."""
    await _require_org_member(org_id, user["id"])
    return await _search_impl(org_id=org_id, q=q, limit=limit)


# ── AI synthesis ────────────────────────────────────────────────────────────
class SynthesizeBody(BaseModel):
    org_id: str
    q: str = Field(..., min_length=2, max_length=400)
    top: list = Field(default_factory=list, description="Hits from /api/search to ground the answer")


@router.post("/synthesize")
async def synthesize(body: SynthesizeBody, user: dict = Depends(_resolve_user)):
    await _require_org_member(body.org_id, user["id"])
    if not body.top:
        # Run a fresh search if the caller didn't pass hits
        data = await _search_impl(org_id=body.org_id, q=body.q, limit=10)
        body.top = data["top"]

    if not body.top:
        return {
            "answer": (
                "I couldn't find anything in your org matching that query. "
                "Try different keywords, or ask a specialist agent directly."
            ),
            "citations": [],
        }

    # Compose a tight context block of the top hits
    parts = []
    for i, h in enumerate(body.top[:10], start=1):
        kind = h.get("kind")
        title = (h.get("title") or "").strip()
        excerpt = (h.get("excerpt") or "").strip()[:480]
        meta = ""
        if kind == "message":
            meta = f" (in conv: {h.get('conversation_title','')})"
        elif kind == "mission":
            meta = f" (status: {h.get('status','')})"
        parts.append(f"[{i}] {kind.upper()} — {title}{meta}\n{excerpt}")
    context = "\n\n".join(parts)

    system = (
        "You are Foundry's in-app search assistant. Given a user's query "
        "and a list of indexed snippets from their org (conversations, "
        "messages, missions, documents, social posts, agents), produce a "
        "concise, useful answer in 3-6 sentences MAX. Cite specific "
        "snippets inline like [1], [2]. Never fabricate — if the snippets "
        "don't answer the query, say so plainly and suggest where to look "
        "next (e.g. 'try asking the Marketing agent directly')."
    )
    user_msg = (
        f"User query:\n{body.q}\n\n"
        f"Indexed snippets:\n{context}\n\n"
        "Write the answer now."
    )

    try:
        chat = _llm_chat(
            api_key=_emergent_key,
            session_id=f"search:{body.org_id}",
            system_message=system,
        ).with_model("openai", "gpt-4.1-mini")
        resp = await chat.send_message(_user_message(text=user_msg))
        answer = (resp or "").strip() or "No answer."
    except Exception as exc:
        logger.warning("search synthesize failed: %s", exc)
        answer = (
            "Synthesis is temporarily unavailable. The raw results above "
            "are still your best starting point."
        )

    return {
        "answer": answer,
        "citations": [
            {
                "n": i,
                "kind": h.get("kind"),
                "title": h.get("title"),
                "url": h.get("url"),
            }
            for i, h in enumerate(body.top[:10], start=1)
        ],
    }
