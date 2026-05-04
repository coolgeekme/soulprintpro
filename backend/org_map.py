"""org_map.py — Mind-map data builder.

Aggregates the existing collections (organizations, teams, conversations,
messages, missions, social_posts, llm_telemetry) into a `{nodes, edges}`
payload for the Phase 1 force-directed graph view at `/app/org/:id/map`.

Design notes:
  • Read-only. No schema changes — just queries against existing data.
  • The graph is intentionally COARSE: Soul → Teams → Agents → recent
    Missions/Conversations. We keep node count under ~200 so D3 stays
    snappy on the lowest-end laptops.
  • Activity weight = message_count + mission_event_count over the
    selected window. Drives node size on the client.
  • LLM cost (when telemetry has rows in the window) drives node color
    intensity — owner instantly sees where money is being spent.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from bson import ObjectId


# Window presets the UI offers (in days). 0 = all time.
ALLOWED_WINDOWS = {1, 7, 30, 90, 0}


def _window_start(days: int) -> Optional[datetime]:
    if days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


async def build_org_map(db, org_id: str, *, window_days: int = 30) -> dict:
    """Return the full graph payload for one org.

    Shape:
        {
          "org": {id, name, soul, brand_assets},
          "window_days": int,
          "generated_at": iso,
          "totals": {
              "teams", "agents_active", "conversations",
              "messages", "missions", "social_posts",
              "llm_cost_usd", "llm_input_tokens", "llm_output_tokens",
          },
          "nodes": [{id, label, kind, weight, cost, ...}],
          "edges": [{source, target, weight, kind}]
        }
    """
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
        "cost": 0.0,
        "subtitle": (org.get("soul_description") or "")[:160],
    }

    nodes: list[dict] = [org_node]
    edges: list[dict] = []

    # ── Teams (and the agents pinned to each team) ────────────────────
    teams_cursor = db.teams.find({"org_id": org_id}, {"name": 1, "agents": 1})
    teams = await teams_cursor.to_list(50)

    # Index team_id -> set of conversation_ids (used to count messages)
    team_conv_ids: dict[str, list[str]] = {}
    all_conv_ids: list[str] = []
    for t in teams:
        tid = str(t["_id"])
        conv_cursor = db.conversations.find({"team_id": tid}, {"_id": 1})
        cids = [str(c["_id"]) async for c in conv_cursor]
        team_conv_ids[tid] = cids
        all_conv_ids.extend(cids)

    # Message counts by conversation (windowed)
    msg_match = {"conversation_id": {"$in": all_conv_ids}}
    if start is not None:
        msg_match["timestamp"] = {"$gte": start}
    msg_counts: dict[str, int] = {}
    if all_conv_ids:
        async for r in db.messages.aggregate([
            {"$match": msg_match},
            {"$group": {"_id": "$conversation_id", "n": {"$sum": 1}}},
        ]):
            msg_counts[r["_id"]] = r["n"]
    # Per-agent message volume (windowed) — for agent node size
    agent_msg_counts: dict[str, int] = {}
    if all_conv_ids:
        async for r in db.messages.aggregate([
            {"$match": {**msg_match, "sender_type": "agent",
                        "agent_id": {"$ne": None}}},
            {"$group": {"_id": "$agent_id", "n": {"$sum": 1}}},
        ]):
            agent_msg_counts[r["_id"]] = r["n"]

    # LLM telemetry totals (windowed) — drives color intensity
    tele_match: dict = {"org_id": org_id}
    if start is not None:
        tele_match["timestamp"] = {"$gte": start}
    cost_by_team: dict[str, float] = {}
    cost_by_user: dict[str, float] = {}
    total_cost = 0.0
    total_in = 0
    total_out = 0
    async for r in db.llm_telemetry.aggregate([
        {"$match": tele_match},
        {"$group": {
            "_id": {"team_id": "$team_id", "user_id": "$user_id"},
            "cost": {"$sum": "$cost"},
            "in_tokens": {"$sum": "$input_tokens"},
            "out_tokens": {"$sum": "$output_tokens"},
        }},
    ]):
        cost = float(r.get("cost") or 0.0)
        tin = int(r.get("in_tokens") or 0)
        tou = int(r.get("out_tokens") or 0)
        tid = (r["_id"] or {}).get("team_id") or ""
        uid = (r["_id"] or {}).get("user_id") or ""
        if tid:
            cost_by_team[tid] = cost_by_team.get(tid, 0.0) + cost
        if uid:
            cost_by_user[uid] = cost_by_user.get(uid, 0.0) + cost
        total_cost += cost
        total_in += tin
        total_out += tou

    # Build team & agent nodes
    agents_seen: set[str] = set()
    total_messages = 0
    for t in teams:
        tid = str(t["_id"])
        cids = team_conv_ids.get(tid, [])
        team_msgs = sum(msg_counts.get(c, 0) for c in cids)
        total_messages += team_msgs
        team_cost = cost_by_team.get(tid, 0.0)
        team_node = {
            "id": f"team:{tid}",
            "label": t.get("name") or "Team",
            "kind": "team",
            "weight": 13 + min(8, team_msgs // 10),
            "cost": round(team_cost, 4),
            "subtitle": f"{team_msgs} msgs · ${team_cost:.2f}",
            "conv_count": len(cids),
        }
        nodes.append(team_node)
        edges.append({"source": org_node["id"], "target": team_node["id"],
                      "weight": max(1, team_msgs), "kind": "team-of"})

        for aid in (t.get("agents") or []):
            n = agent_msg_counts.get(aid, 0)
            if n == 0 and aid in agents_seen:
                # Agent already added under another team and inactive here
                continue
            agents_seen.add(aid)
            agent_node = {
                "id": f"agent:{tid}:{aid}",
                "label": aid.replace("-", " ").title(),
                "kind": "agent",
                "weight": 6 + min(6, n // 4),
                "cost": 0.0,
                "subtitle": f"{n} msgs",
                "agent_id": aid,
            }
            nodes.append(agent_node)
            edges.append({"source": team_node["id"], "target": agent_node["id"],
                          "weight": max(1, n), "kind": "agent-of"})

    # ── Recent missions (last N) ──────────────────────────────────────
    # Use `_id` for sort/window since `created_at` may be absent on older
    # documents — ObjectId encodes the creation timestamp anyway.
    miss_match: dict = {"org_id": org_id}
    if start is not None:
        miss_match["_id"] = {"$gte": ObjectId.from_datetime(start)}
    miss_cursor = db.missions.find(
        miss_match,
        {"goal": 1, "status": 1, "team_id": 1, "lead_agent_id": 1, "created_at": 1},
    ).sort("_id", -1).limit(40)
    missions = await miss_cursor.to_list(40)
    for m in missions:
        mid = str(m["_id"])
        team_link = f"team:{m.get('team_id')}" if m.get("team_id") else org_node["id"]
        # If the team isn't on the map (deleted), fall back to soul
        if not any(n["id"] == team_link for n in nodes):
            team_link = org_node["id"]
        m_node = {
            "id": f"mission:{mid}",
            "label": (m.get("goal") or "Mission")[:60],
            "kind": "mission",
            "status": m.get("status") or "unknown",
            "weight": 8,
            "cost": 0.0,
            "subtitle": f"{m.get('status') or '?'} · lead {m.get('lead_agent_id') or '?'}",
        }
        nodes.append(m_node)
        edges.append({"source": team_link, "target": m_node["id"],
                      "weight": 1, "kind": "mission-of"})

    # ── Recent social posts (last N) ──────────────────────────────────
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
        p_node = {
            "id": f"post:{pid}",
            "label": (p.get("text") or "Post")[:50] or "Post",
            "kind": "post",
            "status": p.get("status") or "draft",
            "platform": p.get("platform") or "any",
            "weight": 5,
            "cost": 0.0,
            "subtitle": f"{p.get('platform') or 'any'} · {p.get('status') or 'draft'}",
        }
        nodes.append(p_node)
        edges.append({"source": org_node["id"], "target": p_node["id"],
                      "weight": 1, "kind": "post-of"})

    # ── Active members (top spenders by LLM cost) ─────────────────────
    if cost_by_user:
        top_users = sorted(cost_by_user.items(), key=lambda x: x[1], reverse=True)[:10]
        # Resolve names
        ids = [u for u, _ in top_users]
        oid_list: list[ObjectId] = []
        for u in ids:
            try:
                oid_list.append(ObjectId(u))
            except Exception:
                pass
        users_doc = {str(u["_id"]): u async for u in db.users.find(
            {"_id": {"$in": oid_list}}, {"name": 1, "email": 1},
        )}
        for uid, cost in top_users:
            u = users_doc.get(uid) or {}
            label = (u.get("name") or u.get("email") or "Member").split("@")[0]
            u_node = {
                "id": f"user:{uid}",
                "label": label,
                "kind": "user",
                "weight": 7 + min(5, int(cost * 30)),
                "cost": round(cost, 4),
                "subtitle": f"${cost:.2f} · {label}",
            }
            nodes.append(u_node)
            edges.append({"source": org_node["id"], "target": u_node["id"],
                          "weight": max(1, int(cost * 100)),
                          "kind": "member-of"})

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
            "llm_cost_usd": round(total_cost, 4),
            "llm_input_tokens": total_in,
            "llm_output_tokens": total_out,
        },
        "nodes": nodes,
        "edges": edges,
    }
