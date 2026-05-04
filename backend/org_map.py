"""org_map.py — Mind-map data builder.

Aggregates the existing collections (organizations, teams, conversations,
messages, missions, social_posts, llm_calls, org_members, users) into a
`{nodes, edges}` payload for the force-directed graph view at
`/app/org/:id/map`.

Design notes (Phase 1 + Phase 2 + the Feb-4 "pull from everyone" update):
  • Every org MEMBER is a node (not just top spenders). Inactive members
    show up as smaller, dimmer nodes so the owner still sees the full
    team but activity stays visually prominent.
  • Every CONVERSATION is a node between its team and messages. Labels
    use the lead agent's *division* (Marketing, Engineering, Design,
    Academic, etc.) as the category tag so the graph reads at a glance
    instead of surfacing raw chat titles.
  • Missions and posts also gain a category tag derived from the lead
    agent's division (missions) or the platform (posts).

Cost is intentionally NOT in the payload. Telemetry is still recorded
via ``llm_logging.py`` for the admin dashboard; the org-facing map
surfaces time + activity only.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from typing import Optional

from bson import ObjectId


ALLOWED_WINDOWS = {1, 7, 30, 90, 0}

# Hard cap so the force simulation stays snappy on 150+ agent orgs.
MAX_CONVERSATION_NODES = 60
MAX_MEMBER_NODES = 60


def _window_start(days: int) -> Optional[datetime]:
    if days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


@lru_cache(maxsize=1)
def _agent_division_index() -> dict[str, str]:
    """Map agent_id -> Division (e.g. 'Marketing', 'Engineering'). Cached
    because the catalogue is immutable at runtime and we hit it once per
    map call at most. Returns an empty dict if the file is missing."""
    path = os.path.join(os.path.dirname(__file__), "agents_catalogue.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            catalogue = json.load(fh)
    except Exception:
        return {}
    idx: dict[str, str] = {}
    for a in catalogue:
        aid = a.get("id")
        div = a.get("division") or a.get("division_key") or ""
        if aid and div:
            idx[aid] = div
    return idx


def _category_for_agent(agent_id: str) -> str:
    """Division label for a single agent id. Falls back to a cleaned
    form of the id prefix (e.g. 'marketing-seo-specialist' -> 'Marketing')
    if the agent isn't in the catalogue — handy for forward-compat with
    future custom user-authored agents."""
    if not agent_id:
        return "General"
    idx = _agent_division_index()
    if agent_id in idx:
        return idx[agent_id]
    prefix = agent_id.split("-", 1)[0] if "-" in agent_id else agent_id
    return prefix.capitalize() or "General"


def _short(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: max(1, n - 1)].rstrip() + "…"


async def build_org_map(db, org_id: str, *, window_days: int = 30) -> dict:
    if window_days not in ALLOWED_WINDOWS:
        window_days = 30
    start = _window_start(window_days)
    org_oid = ObjectId(org_id)
    org = await db.organizations.find_one({"_id": org_oid})
    if not org:
        raise ValueError("organization not found")

    # ── Center: the Soul / company itself ─────────────────────────────
    org_node = {
        "id": f"org:{org_id}",
        "label": org.get("name") or "Company",
        "kind": "soul",
        "weight": 22,
        "time_ms": 0,
        "calls": 0,
        "subtitle": (org.get("soul_description") or "")[:160],
    }
    nodes: list[dict] = [org_node]
    edges: list[dict] = []

    # ── Teams (and the agents pinned to each team) ────────────────────
    teams_cursor = db.teams.find(
        {"org_id": org_id},
        {"name": 1, "agents": 1, "is_general": 1, "is_direct": 1},
    )
    teams = await teams_cursor.to_list(50)

    # Index team_id -> set of conversation_ids
    team_conv_ids: dict[str, list[str]] = {}
    conv_to_team: dict[str, str] = {}
    all_conv_ids: list[str] = []
    for t in teams:
        tid = str(t["_id"])
        conv_cursor = db.conversations.find({"team_id": tid}, {"_id": 1})
        cids = [str(c["_id"]) async for c in conv_cursor]
        team_conv_ids[tid] = cids
        for c in cids:
            conv_to_team[c] = tid
        all_conv_ids.extend(cids)

    # Messages matcher (windowed)
    msg_match: dict = {"conversation_id": {"$in": all_conv_ids}}
    if start is not None:
        msg_match["timestamp"] = {"$gte": start}

    # Per-conversation message count — drives conversation node size AND
    # picks which conversations make the top-N cut under the hard cap.
    msg_count_by_conv: dict[str, int] = {}
    if all_conv_ids:
        async for r in db.messages.aggregate([
            {"$match": msg_match},
            {"$group": {"_id": "$conversation_id", "n": {"$sum": 1}}},
        ]):
            msg_count_by_conv[r["_id"]] = r["n"]

    # Per-agent message volume (windowed) — used for agent node size
    agent_msg_counts: dict[str, int] = {}
    if all_conv_ids:
        async for r in db.messages.aggregate([
            {"$match": {**msg_match, "sender_type": "agent",
                        "agent_id": {"$ne": None}}},
            {"$group": {"_id": "$agent_id", "n": {"$sum": 1}}},
        ]):
            agent_msg_counts[r["_id"]] = r["n"]

    # Dominant-agent per conversation — used to pick the category label.
    # We take the agent who posted the most messages in the conversation
    # as its topical signature.
    dom_agent_by_conv: dict[str, str] = {}
    if all_conv_ids:
        async for r in db.messages.aggregate([
            {"$match": {"conversation_id": {"$in": all_conv_ids},
                        "sender_type": "agent", "agent_id": {"$ne": None}}},
            {"$group": {"_id": {"c": "$conversation_id", "a": "$agent_id"},
                        "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ]):
            cid = r["_id"]["c"]
            if cid not in dom_agent_by_conv:
                dom_agent_by_conv[cid] = r["_id"]["a"]

    # ── LLM telemetry (windowed) — drives Time heatmap ────────────────
    tele_match: dict = {"org_id": org_id, "ok": {"$ne": False}}
    if start is not None:
        tele_match["created_at"] = {"$gte": start}

    time_by_conv: dict[str, int] = {}
    calls_by_conv: dict[str, int] = {}
    async for r in db.llm_calls.aggregate([
        {"$match": {**tele_match, "conversation_id": {"$ne": None}}},
        {"$group": {"_id": "$conversation_id",
                    "time_ms": {"$sum": "$latency_ms"},
                    "calls": {"$sum": 1}}},
    ]):
        time_by_conv[r["_id"]] = int(r.get("time_ms") or 0)
        calls_by_conv[r["_id"]] = int(r.get("calls") or 0)

    time_by_agent: dict[str, int] = {}
    calls_by_agent: dict[str, int] = {}
    async for r in db.llm_calls.aggregate([
        {"$match": {**tele_match, "agent_id": {"$ne": None}}},
        {"$group": {"_id": "$agent_id",
                    "time_ms": {"$sum": "$latency_ms"},
                    "calls": {"$sum": 1}}},
    ]):
        time_by_agent[r["_id"]] = int(r.get("time_ms") or 0)
        calls_by_agent[r["_id"]] = int(r.get("calls") or 0)

    time_by_user: dict[str, int] = {}
    calls_by_user: dict[str, int] = {}
    async for r in db.llm_calls.aggregate([
        {"$match": {**tele_match, "user_id": {"$ne": None}}},
        {"$group": {"_id": "$user_id",
                    "time_ms": {"$sum": "$latency_ms"},
                    "calls": {"$sum": 1}}},
    ]):
        time_by_user[r["_id"]] = int(r.get("time_ms") or 0)
        calls_by_user[r["_id"]] = int(r.get("calls") or 0)

    total_time_ms = 0
    total_calls = 0
    async for r in db.llm_calls.aggregate([
        {"$match": tele_match},
        {"$group": {"_id": None,
                    "time_ms": {"$sum": "$latency_ms"},
                    "calls": {"$sum": 1}}},
    ]):
        total_time_ms = int(r.get("time_ms") or 0)
        total_calls = int(r.get("calls") or 0)

    # Roll conversation-level metrics up to teams
    time_by_team: dict[str, int] = {}
    calls_by_team: dict[str, int] = {}
    for cid, t_ms in time_by_conv.items():
        tid = conv_to_team.get(cid)
        if not tid:
            continue
        time_by_team[tid] = time_by_team.get(tid, 0) + t_ms
        calls_by_team[tid] = calls_by_team.get(tid, 0) + calls_by_conv.get(cid, 0)

    # ── Build team nodes + AGENT nodes ────────────────────────────────
    agents_seen: set[str] = set()
    total_messages = 0
    team_nodes_by_id: dict[str, dict] = {}
    for t in teams:
        tid = str(t["_id"])
        cids = team_conv_ids.get(tid, [])
        team_msgs = sum(msg_count_by_conv.get(c, 0) for c in cids)
        total_messages += team_msgs
        team_node = {
            "id": f"team:{tid}",
            "label": t.get("name") or "Team",
            "kind": "team",
            "weight": 13 + min(8, team_msgs // 10),
            "time_ms": time_by_team.get(tid, 0),
            "calls": calls_by_team.get(tid, 0),
            "subtitle": f"{team_msgs} msgs · {calls_by_team.get(tid, 0)} LLM calls",
            "conv_count": len(cids),
        }
        nodes.append(team_node)
        team_nodes_by_id[tid] = team_node
        edges.append({"source": org_node["id"], "target": team_node["id"],
                      "weight": max(1, team_msgs), "kind": "team-of"})

        for aid in (t.get("agents") or []):
            n = agent_msg_counts.get(aid, 0)
            a_time = time_by_agent.get(aid, 0)
            a_calls = calls_by_agent.get(aid, 0)
            if n == 0 and a_calls == 0 and aid in agents_seen:
                continue
            agents_seen.add(aid)
            agent_node = {
                "id": f"agent:{tid}:{aid}",
                "label": aid.replace("-", " ").title(),
                "kind": "agent",
                "weight": 6 + min(6, n // 4),
                "time_ms": a_time,
                "calls": a_calls,
                "subtitle": f"{_category_for_agent(aid)} · {n} msgs",
                "category": _category_for_agent(aid),
                "agent_id": aid,
            }
            nodes.append(agent_node)
            edges.append({"source": team_node["id"], "target": agent_node["id"],
                          "weight": max(1, n), "kind": "agent-of"})

    # ── Build CONVERSATION nodes (category-labelled) ──────────────────
    # One node per conversation up to MAX_CONVERSATION_NODES; picks the
    # busiest-in-window first so the owner sees what's actually active.
    # Each node is clustered under its team and labelled by the dominant
    # agent's division ("Marketing · General chat", "Engineering · API redesign").
    conv_docs: list[dict] = []
    if all_conv_ids:
        async for c in db.conversations.find(
            {"_id": {"$in": [ObjectId(x) for x in all_conv_ids]}},
            {"title": 1, "team_id": 1, "is_general": 1, "is_direct": 1,
             "agent_ids": 1, "created_at": 1},
        ):
            conv_docs.append(c)
    # Sort by in-window message count desc; ties broken by recency (by _id).
    conv_docs.sort(
        key=lambda c: (msg_count_by_conv.get(str(c["_id"]), 0), str(c["_id"])),
        reverse=True,
    )
    conv_nodes_built = 0
    for c in conv_docs:
        cid = str(c["_id"])
        tid = c.get("team_id")
        team_node = team_nodes_by_id.get(tid)
        if not team_node:
            continue
        n_msgs = msg_count_by_conv.get(cid, 0)
        # Cap the graph — once we've added MAX_CONVERSATION_NODES, we stop.
        # But always include the "general"-flagged conversation so the
        # Chief-of-Staff chat is visible even on quiet windows.
        if conv_nodes_built >= MAX_CONVERSATION_NODES and not c.get("is_general"):
            continue
        # Category label: prefer dominant agent's division; fall back to
        # the first pinned agent_ids entry; finally "General".
        lead_agent = dom_agent_by_conv.get(cid) or \
                     ((c.get("agent_ids") or [None])[0])
        category = _category_for_agent(lead_agent) if lead_agent else "General"
        # Short name: raw title truncated; for direct/general chats we
        # show a prettier tag since titles are often "General" / "Direct".
        raw_title = (c.get("title") or "").strip()
        if c.get("is_general"):
            pretty = "Chief of Staff"
        elif c.get("is_direct"):
            pretty = f"Direct · {_short(raw_title, 22)}" if raw_title else "Direct chat"
        else:
            pretty = _short(raw_title, 28) or "Conversation"
        label = f"{category} · {pretty}"
        conv_node = {
            "id": f"conv:{cid}",
            "label": label,
            "kind": "conversation",
            "category": category,
            "weight": 5 + min(7, n_msgs // 8),
            "time_ms": time_by_conv.get(cid, 0),
            "calls": calls_by_conv.get(cid, 0),
            "subtitle": f"{n_msgs} msgs · {calls_by_conv.get(cid, 0)} LLM calls",
        }
        nodes.append(conv_node)
        edges.append({"source": team_node["id"], "target": conv_node["id"],
                      "weight": max(1, n_msgs), "kind": "conv-of"})
        conv_nodes_built += 1

    # ── Recent missions ───────────────────────────────────────────────
    miss_match: dict = {"org_id": org_id}
    if start is not None:
        miss_match["_id"] = {"$gte": ObjectId.from_datetime(start)}
    miss_cursor = db.missions.find(
        miss_match,
        {"goal": 1, "status": 1, "team_id": 1, "lead_agent_id": 1, "created_at": 1},
    ).sort("_id", -1).limit(40)
    missions = await miss_cursor.to_list(40)
    mission_telemetry: dict[str, dict] = {}
    if missions:
        mids = [str(m["_id"]) for m in missions]
        async for r in db.llm_calls.aggregate([
            {"$match": {**tele_match, "mission_id": {"$in": mids}}},
            {"$group": {"_id": "$mission_id",
                        "time_ms": {"$sum": "$latency_ms"},
                        "calls": {"$sum": 1}}},
        ]):
            mission_telemetry[r["_id"]] = {
                "time_ms": int(r.get("time_ms") or 0),
                "calls": int(r.get("calls") or 0),
            }

    for m in missions:
        mid = str(m["_id"])
        team_link = f"team:{m.get('team_id')}" if m.get("team_id") else org_node["id"]
        if not any(n["id"] == team_link for n in nodes):
            team_link = org_node["id"]
        mt = mission_telemetry.get(mid, {})
        lead = m.get("lead_agent_id") or ""
        category = _category_for_agent(lead) if lead else "Mission"
        goal_words = (m.get("goal") or "Mission").split()
        short_goal = " ".join(goal_words[:4])
        m_node = {
            "id": f"mission:{mid}",
            "label": f"{category} · {_short(short_goal, 30)}",
            "kind": "mission",
            "status": m.get("status") or "unknown",
            "category": category,
            "weight": 8,
            "time_ms": int(mt.get("time_ms", 0)),
            "calls": int(mt.get("calls", 0)),
            "subtitle": f"{m.get('status') or '?'} · {mt.get('calls', 0)} LLM calls",
        }
        nodes.append(m_node)
        edges.append({"source": team_link, "target": m_node["id"],
                      "weight": 1, "kind": "mission-of"})

    # ── Recent social posts ───────────────────────────────────────────
    post_match: dict = {"org_id": org_id}
    if start is not None:
        post_match["_id"] = {"$gte": ObjectId.from_datetime(start)}
    post_cursor = db.social_posts.find(
        post_match,
        {"text": 1, "status": 1, "platform": 1, "created_at": 1},
    ).sort("_id", -1).limit(20)
    posts = await post_cursor.to_list(20)
    for p in posts:
        pid = str(p["_id"])
        platform = (p.get("platform") or "any").capitalize()
        status = (p.get("status") or "draft").capitalize()
        p_node = {
            "id": f"post:{pid}",
            "label": f"{platform} · {status}",
            "kind": "post",
            "status": p.get("status") or "draft",
            "platform": p.get("platform") or "any",
            "category": platform,
            "weight": 5,
            "time_ms": 0,
            "calls": 0,
            "subtitle": f"{platform} · {status}",
        }
        nodes.append(p_node)
        edges.append({"source": org_node["id"], "target": p_node["id"],
                      "weight": 1, "kind": "post-of"})

    # ── ALL org members (not just top spenders) ───────────────────────
    members = await db.org_members.find(
        {"org_id": org_id}, {"user_id": 1, "role": 1},
    ).to_list(200)
    member_ids = [m["user_id"] for m in members]
    member_oids: list[ObjectId] = []
    for uid in member_ids:
        try:
            member_oids.append(ObjectId(uid))
        except Exception:
            pass
    users_doc = {str(u["_id"]): u async for u in db.users.find(
        {"_id": {"$in": member_oids}}, {"name": 1, "email": 1},
    )}
    # Rank: most active first (by LLM call count, then msg count).
    def _member_activity(uid: str) -> int:
        return calls_by_user.get(uid, 0) + (0 if uid not in time_by_user else 1)
    members.sort(key=lambda m: _member_activity(m["user_id"]), reverse=True)
    # Soft cap — with 60 members + all teams/agents/convs we still stay
    # well under the 200-node ceiling.
    for m in members[:MAX_MEMBER_NODES]:
        uid = m["user_id"]
        u = users_doc.get(uid) or {}
        label = (u.get("name") or u.get("email") or "Member").split("@")[0]
        t_ms = int(time_by_user.get(uid, 0))
        u_calls = int(calls_by_user.get(uid, 0))
        role = m.get("role") or "member"
        weight = 5 + min(7, u_calls // 4)
        u_node = {
            "id": f"user:{uid}",
            "label": label,
            "kind": "user",
            "weight": weight,
            "time_ms": t_ms,
            "calls": u_calls,
            "role": role,
            "inactive": u_calls == 0,
            "subtitle": (
                f"{role.capitalize()} · {u_calls} LLM calls" if u_calls
                else f"{role.capitalize()} · no activity yet"
            ),
        }
        nodes.append(u_node)
        edges.append({"source": org_node["id"], "target": u_node["id"],
                      "weight": max(1, u_calls),
                      "kind": "member-of"})

    # Heatmap normalisation max
    time_ms_max = max((n.get("time_ms") or 0) for n in nodes) if nodes else 0

    return {
        "org": {
            "id": org_id,
            "name": org.get("name") or "Company",
            "soul": (org.get("soul_description") or "")[:400],
        },
        "window_days": window_days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "teams": len(teams),
            "agents_active": len(agents_seen),
            "conversations": len(all_conv_ids),
            "messages": total_messages,
            "missions": len(missions),
            "social_posts": len(posts),
            "members": len(members),
            "llm_calls": total_calls,
            "llm_time_ms": total_time_ms,
            "time_ms_max": int(time_ms_max),
        },
        "nodes": nodes,
        "edges": edges,
    }
