"""Personal Concierge — read-only, cross-org assistant for Telegram.

This is the fallback agent when a Telegram inbound message doesn't
explicitly route to one org. Unlike a normal Chief of Staff (which is
org-scoped and can mutate org data), the Personal Concierge:

  • Reads SUMMARIES across every org the user belongs to
  • NEVER calls a tool that mutates anything
  • If a request requires actual work (write to HubSpot, publish a post,
    update a doc), it tells the user which org to address and stops.

Tool surface exposed to the LLM:
  - list_orgs()
  - get_orgs_overview(window_days)
  - get_my_calendar(window_hours)
  - punt_to_org(name, request)

Each tool returns plain JSON the model can reason over. The concierge
loop is a small ReAct-style controller — not the full Composio agent
loop — because it only needs ~3 tool calls per turn and we'd rather
keep latency low for Telegram users on the go.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId

logger = logging.getLogger("foundry.personal_concierge")

# Hard-cap on the tool loop so a misbehaving LLM can't spin forever.
MAX_TURNS = 4


# ── Data gathering primitives ──────────────────────────────────────────────
async def _user_orgs(db, user_id: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    async for m in db.org_members.find(
        {"user_id": user_id},
        {"_id": 0, "org_id": 1, "role": 1},
    ):
        try:
            org = await db.organizations.find_one(
                {"_id": ObjectId(m["org_id"])},
                {"_id": 1, "name": 1},
            )
        except Exception:
            continue
        if org:
            out.append({
                "id": str(org["_id"]),
                "name": org.get("name", ""),
                "role": m.get("role"),
            })
    return out


async def _gather_overview(db, user_id: str, window_days: int = 7) -> Dict[str, Any]:
    """Per-org summary of: active missions, scheduled posts, stalled
    drafts. Bounded by `window_days` so we don't drown the model in
    history.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    orgs = await _user_orgs(db, user_id)
    out: List[Dict[str, Any]] = []
    for org in orgs:
        oid = org["id"]
        # Active missions — running OR finished in the window.
        active_missions = await db.missions.count_documents({
            "org_id": oid, "created_by": user_id,
            "status": {"$in": ["pending", "running"]},
        })
        recent_missions = []
        async for m in db.missions.find(
            {"org_id": oid, "created_by": user_id,
             "$or": [
                 {"status": {"$in": ["pending", "running"]}},
                 {"finished_at": {"$gte": cutoff}},
             ]},
            {"_id": 1, "objective": 1, "status": 1, "finished_at": 1},
        ).limit(10):
            recent_missions.append({
                "id": str(m["_id"]),
                "objective": (m.get("objective") or "")[:120],
                "status": m.get("status"),
            })
        # Scheduled / draft / failed posts in window.
        scheduled_posts = []
        async for p in db.social_posts.find(
            {"org_id": oid, "author_id": user_id,
             "scheduled_for": {"$gte": datetime.now(timezone.utc)},
             "status": {"$in": ["draft", "scheduled"]}},
            {"_id": 1, "text": 1, "platforms": 1, "scheduled_for": 1, "status": 1},
        ).sort("scheduled_for", 1).limit(10):
            scheduled_posts.append({
                "id": str(p["_id"]),
                "text": (p.get("text") or "")[:120],
                "platforms": p.get("platforms") or [],
                "scheduled_for": (
                    p["scheduled_for"].isoformat()
                    if isinstance(p.get("scheduled_for"), datetime) else None
                ),
                "status": p.get("status"),
            })
        stalled_drafts = await db.social_posts.count_documents({
            "org_id": oid, "author_id": user_id, "status": "draft",
            "created_at": {"$lt": datetime.now(timezone.utc) - timedelta(days=7)},
        })
        failed_recent = await db.social_posts.count_documents({
            "org_id": oid, "author_id": user_id,
            "status": {"$in": ["failed", "partial"]},
            "publish_finished_at": {"$gte": cutoff},
        })
        out.append({
            "org_id": oid,
            "org_name": org["name"],
            "active_missions": active_missions,
            "recent_missions": recent_missions,
            "scheduled_posts": scheduled_posts,
            "stalled_drafts": stalled_drafts,
            "failed_posts_recent": failed_recent,
        })
    return {"orgs": out, "generated_at": datetime.now(timezone.utc).isoformat()}


async def _gather_calendar(db, user_id: str, window_hours: int = 24) -> Dict[str, Any]:
    """Walk every org the user belongs to that has an ACTIVE Google
    Calendar Composio connection, fetch the next `window_hours` of
    events, and return a unified list.

    Errors on one org are isolated — a HubSpot-only org with no
    calendar simply contributes nothing.
    """
    from composio_integration import composio_user_id, execute_tool
    orgs = await _user_orgs(db, user_id)
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=max(1, min(168, int(window_hours))))
    args = {
        "time_min": now.isoformat(),
        "time_max": horizon.isoformat(),
        "max_results": 25,
        "single_events": True,
        "order_by": "startTime",
    }
    events: List[Dict[str, Any]] = []
    orgs_checked = 0
    for org in orgs:
        oid = org["id"]
        cu = composio_user_id(oid, user_id)
        conn = await db.composio_connections.find_one(
            {"composio_user_id": cu, "toolkit": "googlecalendar", "status": "ACTIVE"},
            {"_id": 0, "connection_id": 1},
        )
        if not conn:
            continue
        orgs_checked += 1
        try:
            res = await execute_tool(db, oid, user_id, "GOOGLECALENDAR_FIND_EVENTS", args)
        except Exception as exc:
            logger.warning("calendar fetch failed for %s: %s", oid, exc)
            continue
        if not res.get("success"):
            continue
        payload = res.get("response") or {}
        items = (
            payload.get("items")
            or payload.get("events")
            or (payload.get("data") or {}).get("items")
            or []
        )
        for ev in items[:25]:
            start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
            end = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date")
            events.append({
                "org_name": org["name"],
                "summary": ev.get("summary") or "(no title)",
                "start": start,
                "end": end,
                "location": ev.get("location") or "",
                "attendees": [a.get("email") for a in (ev.get("attendees") or [])][:6],
                "html_link": ev.get("htmlLink") or "",
            })
    # Sort merged events chronologically. Some may have date-only
    # values; treat those as start-of-day for sorting.
    def _sort_key(e):
        s = e.get("start") or ""
        return s
    events.sort(key=_sort_key)
    return {
        "orgs_with_calendar": orgs_checked,
        "orgs_total": len(orgs),
        "events": events,
        "window_hours": window_hours,
    }


async def _gather_inbox(
    db, user_id: str, *,
    query: str = "",
    max_results: int = 15,
    window_hours: int = 48,
) -> Dict[str, Any]:
    """Pull recent Gmail messages across every org with an ACTIVE
    Gmail Composio connection.

    `query` is passed through as the Gmail search query syntax
    (``is:unread``, ``from:acme.com``, ``after:2026/02/16`` etc).
    Empty query → newest `max_results` in the window across all orgs.
    """
    from composio_integration import composio_user_id, execute_tool
    orgs = await _user_orgs(db, user_id)
    # Time-windowed default — Gmail's after: takes YYYY/MM/DD, no time.
    if not query:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, min(168, int(window_hours))))
        query = f"after:{cutoff.strftime('%Y/%m/%d')}"
    args = {
        "query": query,
        "max_results": max(1, min(25, int(max_results))),
    }
    messages: List[Dict[str, Any]] = []
    orgs_checked = 0
    for org in orgs:
        oid = org["id"]
        cu = composio_user_id(oid, user_id)
        conn = await db.composio_connections.find_one(
            {"composio_user_id": cu, "toolkit": "gmail", "status": "ACTIVE"},
            {"_id": 0, "connection_id": 1},
        )
        if not conn:
            continue
        orgs_checked += 1
        try:
            res = await execute_tool(db, oid, user_id, "GMAIL_FETCH_EMAILS", args)
        except Exception as exc:
            logger.warning("gmail fetch failed for %s: %s", oid, exc)
            continue
        if not res.get("success"):
            continue
        payload = res.get("response") or {}
        items = (
            payload.get("messages")
            or (payload.get("data") or {}).get("messages")
            or payload.get("items")
            or []
        )
        for m in items[: max_results]:
            messages.append({
                "org_name": org["name"],
                "id": m.get("messageId") or m.get("id") or "",
                "thread_id": m.get("threadId") or "",
                "from": m.get("from") or m.get("sender") or "",
                "to": m.get("to") or "",
                "subject": (m.get("subject") or "(no subject)")[:140],
                "snippet": (m.get("messageText") or m.get("snippet") or "")[:240],
                "received": m.get("messageTimestamp") or m.get("date") or "",
                "label_ids": m.get("labelIds") or [],
            })
    return {
        "orgs_with_gmail": orgs_checked,
        "orgs_total": len(orgs),
        "messages": messages,
        "query": query,
    }


async def _read_email(db, user_id: str, *, message_id: str) -> Dict[str, Any]:
    """Fetch the full body of a single Gmail message. Tries each Gmail-
    connected org until one returns the message — Gmail message ids
    are unique per Google account, so only the right org will hit."""
    from composio_integration import composio_user_id, execute_tool
    orgs = await _user_orgs(db, user_id)
    for org in orgs:
        oid = org["id"]
        cu = composio_user_id(oid, user_id)
        conn = await db.composio_connections.find_one(
            {"composio_user_id": cu, "toolkit": "gmail", "status": "ACTIVE"},
            {"_id": 0, "connection_id": 1},
        )
        if not conn:
            continue
        try:
            res = await execute_tool(
                db, oid, user_id, "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
                {"message_id": message_id, "format": "full"},
            )
        except Exception:
            continue
        if not res.get("success"):
            continue
        payload = res.get("response") or {}
        body = (
            payload.get("messageText")
            or payload.get("body")
            or (payload.get("data") or {}).get("messageText")
            or ""
        )
        if body:
            return {
                "org_name": org["name"],
                "id": payload.get("messageId") or message_id,
                "from": payload.get("from") or "",
                "to": payload.get("to") or "",
                "subject": payload.get("subject") or "",
                "body": body[:8000],
            }
    return {"error": "Message not found in any of your connected Gmail accounts."}


async def _search_docs(
    db, user_id: str, *,
    query: str,
    max_results: int = 10,
) -> Dict[str, Any]:
    """Case-insensitive substring search across every org's Documents
    library. Cheap regex over `original_name` + `title` + `preview` —
    fine for the volumes Foundry sees today (low hundreds of docs per
    org). Upgrade to text-index when corpora grow."""
    import re as _re
    if not (query or "").strip():
        return {"error": "query is required"}
    orgs = await _user_orgs(db, user_id)
    org_ids = [o["id"] for o in orgs]
    if not org_ids:
        return {"results": [], "orgs_searched": 0}
    name_map = {o["id"]: o["name"] for o in orgs}
    pattern = _re.escape(query.strip())
    cur = db.documents.find(
        {
            "org_id": {"$in": org_ids},
            "$or": [
                {"original_name": {"$regex": pattern, "$options": "i"}},
                {"title": {"$regex": pattern, "$options": "i"}},
                {"preview": {"$regex": pattern, "$options": "i"}},
            ],
        },
        {"_id": 0, "id": 1, "org_id": 1, "original_name": 1, "title": 1,
         "kind": 1, "preview": 1, "created_at": 1, "size": 1, "mime": 1},
    ).sort("created_at", -1).limit(max(1, min(50, int(max_results))))
    results = []
    async for d in cur:
        results.append({
            "org_name": name_map.get(d.get("org_id"), ""),
            "id": d.get("id"),
            "name": d.get("original_name") or d.get("title"),
            "title": d.get("title"),
            "kind": d.get("kind"),
            "mime": d.get("mime"),
            "size": d.get("size"),
            "preview": (d.get("preview") or "")[:200],
        })
    return {
        "query": query,
        "orgs_searched": len(orgs),
        "results": results,
    }


# ── Tool dispatch ──────────────────────────────────────────────────────────
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_orgs",
            "description": "List every org the user belongs to with their role. Cheap, prefer calling this first when the user asks 'what orgs do I have'.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_orgs_overview",
            "description": (
                "Summarise activity across every org the user belongs to: "
                "active missions, scheduled posts, stalled drafts, recent failures. "
                "Use this for 'what's on my plate', 'where am I behind', or open-ended status questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_days": {
                        "type": "integer",
                        "description": "Look-back window in days (1-30). Defaults to 7.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_calendar",
            "description": (
                "Pull upcoming Google Calendar events from EVERY org that has a connected "
                "Google Calendar. Use this when the user asks about their agenda, schedule, "
                "meetings, today's calendar, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_hours": {
                        "type": "integer",
                        "description": "Hours ahead to scan (1-168). Defaults to 24 (today).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_inbox",
            "description": (
                "Pull recent Gmail messages across EVERY org with a connected Gmail account. "
                "Use this for 'what's in my inbox', 'any new emails from X', 'show me unread', "
                "etc. `query` accepts Gmail search syntax (is:unread, from:domain.com, "
                "subject:invoice, has:attachment, etc). Empty query → recent messages in the window."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Gmail search syntax. Empty = recent."},
                    "max_results": {"type": "integer", "description": "1-25. Defaults to 15."},
                    "window_hours": {"type": "integer", "description": "When query is empty, look-back in hours (1-168). Defaults to 48."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": (
                "Fetch the full body of a single Gmail message by its message_id (obtained "
                "from `get_my_inbox`). Use this when the user asks 'what does that email "
                "say', 'show me the details', or before drafting a reply."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                },
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_my_docs",
            "description": (
                "Search the user's Document library ACROSS every org by filename, title, "
                "or content preview. Use this for 'find the brief on X', 'where's the "
                "deck for the Y launch', 'pull the contract'. Returns docs with the org "
                "they live in so the user knows where to open them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "description": "1-50. Defaults to 10."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "punt_to_org",
            "description": (
                "Tell the user that their request requires an org-scoped Chief of Staff "
                "(e.g. they asked to add a HubSpot lead, send an email, or otherwise mutate "
                "data). NEVER use this for read-only questions you can answer yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "org_name": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["org_name", "reason"],
            },
        },
    },
]


async def _dispatch(db, user_id: str, name: str, args: Dict[str, Any]) -> str:
    if name == "list_orgs":
        return json.dumps(await _user_orgs(db, user_id))
    if name == "get_orgs_overview":
        days = int(args.get("window_days") or 7)
        return json.dumps(await _gather_overview(db, user_id, window_days=days))
    if name == "get_my_calendar":
        hrs = int(args.get("window_hours") or 24)
        return json.dumps(await _gather_calendar(db, user_id, window_hours=hrs))
    if name == "get_my_inbox":
        return json.dumps(await _gather_inbox(
            db, user_id,
            query=(args.get("query") or "").strip(),
            max_results=int(args.get("max_results") or 15),
            window_hours=int(args.get("window_hours") or 48),
        ))
    if name == "read_email":
        mid = (args.get("message_id") or "").strip()
        if not mid:
            return json.dumps({"error": "message_id is required"})
        return json.dumps(await _read_email(db, user_id, message_id=mid))
    if name == "search_my_docs":
        return json.dumps(await _search_docs(
            db, user_id,
            query=(args.get("query") or "").strip(),
            max_results=int(args.get("max_results") or 10),
        ))
    if name == "punt_to_org":
        return json.dumps({
            "ok": True,
            "instruction_to_user": (
                f"For this request, address it to the {args.get('org_name')} Chief of Staff. "
                "Either prefix your next message with that org's name, or use /org "
                f"{args.get('org_name')} <your message>."
            ),
        })
    return json.dumps({"error": f"Unknown tool: {name}"})


# ── Main entry point ───────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are the user's Personal Chief of Staff inside Foundry, reached through Telegram. You are organisation-agnostic by default — the user talks to you the way they would to a trusted human assistant who happens to have READ access to everything across every org they belong to.

You can:
- Summarise activity across every org (`get_orgs_overview`)
- Read their full calendar across every Google Calendar they've connected (`get_my_calendar`)
- Read recent Gmail across every Gmail account they've connected (`get_my_inbox`, `read_email`)
- Search documents across every org's library (`search_my_docs`)
- List the orgs they're in (`list_orgs`)
- **Answer general questions** (world knowledge, advice, casual conversation) without calling any tool — just talk naturally
- **Draft email replies, message drafts, talking points** — output them as text the user can copy. This is OK; it's just text, not a write to any system.

What you CANNOT do:
- Send emails, publish posts, mutate calendar events, create CRM rows, or otherwise WRITE to any third-party system. If the user asks for a write action, use `punt_to_org` to tell them which org to address the request to (e.g. "Send this via the Acme Org Chief of Staff: /org Acme reply to that email saying yes"). Drafting a reply for the user to copy is fine — only the actual `send` is gated.

Tone & format:
- Talk like a sharp human assistant. Plain English. No emojis unless echoing the user's tone.
- Telegram renders simple HTML — use <b>bold</b>, <i>italics</i> sparingly. Avoid markdown headings/tables.
- Keep replies tight (3-8 lines for status; longer only when the user asked for detail).
- Group findings by org name only when it actually helps clarity. For "what's my agenda today" just list times + titles chronologically; org name is metadata.
- Reference timestamps in user-friendly form ("today at 3:00 PM"), not raw ISO.
- When you draft a reply for the user, include only the draft body (no "Here's a draft:" preamble). The user wants something they can paste.

Routing nuance:
- A `/org <name>` prefix means the user wants the org-scoped CoS, NOT you. They shouldn't be here in that case — but if they do reach you with an action request like "send Acme an email", use `punt_to_org`.
- If the user says "switch to <org>" or "from now on assume <org>", tell them they can use `/org <name> ...` going forward; you don't carry org state."""


async def respond_personal(db, *, user_id: str, message: str) -> str:
    """Run the Personal Concierge against `message`. Returns reply text."""
    key = os.environ.get("EMERGENT_LLM_KEY")
    if not key:
        return "⚠️ EMERGENT_LLM_KEY isn't configured on this deploy — Personal Concierge can't think yet."
    # Use the same client other places use so usage telemetry rolls up
    # consistently. We talk to gpt-4o-mini directly via the OpenAI-compatible
    # endpoint baked into the Emergent LLM gateway.
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key=key,
        base_url="https://integrations.emergentagent.com/llm/v1",
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]
    for _ in range(MAX_TURNS):
        try:
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
                temperature=0.3,
            )
        except Exception as exc:
            logger.exception("Personal concierge LLM call failed")
            return f"⚠️ I hit an error reaching the model: {exc}"
        choice = resp.choices[0]
        msg = choice.message
        if not getattr(msg, "tool_calls", None):
            return (msg.content or "").strip() or "Done."
        # Tool call branch — execute every requested tool, append
        # results, loop. Limited to MAX_TURNS to bound cost.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            result = await _dispatch(db, user_id, tc.function.name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })
    return "I'm still gathering — try again in a moment."
