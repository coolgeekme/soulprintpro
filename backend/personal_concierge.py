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
SYSTEM_PROMPT = """You are the user's Personal Concierge inside Foundry, reached through Telegram.

You have READ-ONLY visibility across every org the user belongs to. You can summarise activity, list calendar events, and point them to which org to focus on. You CANNOT mutate org data — no creating leads, sending emails, publishing posts, etc. If a request requires action on a specific org, use the `punt_to_org` tool and stop.

When answering:
- Use plain English. Telegram doesn't render fancy markdown well — use simple HTML <b> for emphasis if needed.
- Keep replies tight (3-8 lines for status questions, longer only when the user asked for detail).
- Group findings by org name when relevant.
- When the user asks about their agenda/schedule/calendar, call `get_my_calendar` first.
- When the user asks "what's on my plate", "where am I behind", or open-ended status, call `get_orgs_overview` first.
- Reference timestamps in user-friendly form (e.g. "today at 3:00 PM"), not raw ISO strings.
- If the user clearly wants an action (write, send, create, delete, publish, schedule), respond via `punt_to_org` and tell them which org.
- If you don't know which org, ask. Never guess on a write operation."""


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
