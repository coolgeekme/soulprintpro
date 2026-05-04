"""org_map.py — Mind-map data builder.

Aggregates the existing collections (organizations, teams, conversations,
messages, missions, social_posts, llm_calls) into a `{nodes, edges}`
payload for the force-directed graph view at `/app/org/:id/map`.

Phase 1 (graph topology + activity sizing) — DONE.
Phase 2 (cost & latency heatmap)             — THIS FILE.

Phase 2 additions:
  • Per-node ``cost`` and ``time_ms`` so the client can paint a heatmap
    colored by spend or wall-clock latency.
  • Org-level ``cost_max`` / ``time_ms_max`` so the client can normalise
    each node's intensity into a 0..1 ratio without re-walking the data.
  • Read from ``llm_calls`` (the actual telemetry collection) using the
    real field names — earlier code referenced a non-existent
    ``llm_telemetry`` collection so cost was always 0.
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
          "org": {id, name, soul},
          "window_days": int,
          "generated_at": iso,
          "totals": {
              "teams", "agents_active", "conversations",
              "messages", "missions", "social_posts",
              "llm_cost_usd", "llm_input_tokens", "llm_output_tokens",
              "llm_calls", "llm_time_ms",
              "cost_max", "time_ms_max",     # for heatmap normalisation
          },
          "nodes": [{id, label, kind, weight, cost, time_ms, calls, ...}],
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
        "time_ms": 0,
        "calls": 0,
        "subtitle": (org.get("soul_description") or "")[:160],
    }

    nodes: list[dict] = [org_node]
    edges: list[dict] = []

    # ── Teams (and the agents pinned to each team) ────────────────────
    teams_cursor = db.teams.find({"org_id": org_id}, {"name": 1, "agents": 1})
    teams = await teams_cursor.to_list(50)

    # Index team_id -> set of conversation_ids (used to count messages and
    # to attribute LLM spend back to a team via conversation_id).
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

    # Message counts by conversation (windowed)
    msg_match: dict = {"conversation_id": {"$in": all_conv_ids}}
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

    # ── LLM telemetry (windowed) — drives cost+time heatmap ───────────
    # Real collection is `llm_calls`. Fields: cost_usd, prompt_tokens,
    # completion_tokens, latency_ms, created_at.
    tele_match: dict = {"org_id": org_id, "ok": {"$ne": False}}
    if start is not None:
        tele_match["created_at"] = {"$gte": start}

    # Per-conversation aggregation (we resolve team_id via conv_to_team)
    cost_by_conv: dict[str, float] = {}
    time_by_conv: dict[str, int] = {}
    calls_by_conv: dict[str, int] = {}
    async for r in db.llm_calls.aggregate([
        {"$match": {**tele_match, "conversation_id": {"$ne": None}}},
        {"$group": {
            "_id": "$conversation_id",
            "cost": {"$sum": "$cost_usd"},
            "time_ms": {"$sum": "$latency_ms"},
            "calls": {"$sum": 1},
        }},
    ]):
        cid = r["_id"]
        cost_by_conv[cid] = float(r.get("cost") or 0.0)
        time_by_conv[cid] = int(r.get("time_ms") or 0)
        calls_by_conv[cid] = int(r.get("calls") or 0)

    # Per-agent aggregation (across the whole org — covers chat & missions)
    cost_by_agent: dict[str, float] = {}
    time_by_agent: dict[str, int] = {}
    calls_by_agent: dict[str, int] = {}
    async for r in db.llm_calls.aggregate([
        {"$match": {**tele_match, "agent_id": {"$ne": None}}},
        {"$group": {
            "_id": "$agent_id",
            "cost": {"$sum": "$cost_usd"},
            "time_ms": {"$sum": "$latency_ms"},
            "calls": {"$sum": 1},
        }},
    ]):
        aid = r["_id"]
        cost_by_agent[aid] = float(r.get("cost") or 0.0)
        time_by_agent[aid] = int(r.get("time_ms") or 0)
        calls_by_agent[aid] = int(r.get("calls") or 0)

    # Per-user aggregation
    cost_by_user: dict[str, float] = {}
    time_by_user: dict[str, int] = {}
    async for r in db.llm_calls.aggregate([
        {"$match": {**tele_match, "user_id": {"$ne": None}}},
        {"$group": {
            "_id": "$user_id",
            "cost": {"$sum": "$cost_usd"},
            "time_ms": {"$sum": "$latency_ms"},
        }},
    ]):
        uid = r["_id"]
        cost_by_user[uid] = float(r.get("cost") or 0.0)
        time_by_user[uid] = int(r.get("time_ms") or 0)

    # Org-level totals (covers calls without conversation_id too — e.g.
    # standalone routing/soul-gen/style-guide calls).
    total_cost = 0.0
    total_time_ms = 0
    total_calls = 0
    total_in = 0
    total_out = 0
    async for r in db.llm_calls.aggregate([
        {"$match": tele_match},
        {"$group": {
            "_id": None,
            "cost": {"$sum": "$cost_usd"},
            "time_ms": {"$sum": "$latency_ms"},
            "calls": {"$sum": 1},
            "in_tokens": {"$sum": "$prompt_tokens"},
            "out_tokens": {"$sum": "$completion_tokens"},
        }},
    ]):
        total_cost = float(r.get("cost") or 0.0)
        total_time_ms = int(r.get("time_ms") or 0)
        total_calls = int(r.get("calls") or 0)
        total_in = int(r.get("in_tokens") or 0)
        total_out = int(r.get("out_tokens") or 0)

    # Roll conversation-level metrics up to teams.
    cost_by_team: dict[str, float] = {}
    time_by_team: dict[str, int] = {}
    calls_by_team: dict[str, int] = {}
    for cid, c in cost_by_conv.items():
        tid = conv_to_team.get(cid)
        if not tid:
            continue
        cost_by_team[tid] = cost_by_team.get(tid, 0.0) + c
        time_by_team[tid] = time_by_team.get(tid, 0) + time_by_conv.get(cid, 0)
        calls_by_team[tid] = calls_by_team.get(tid, 0) + calls_by_conv.get(cid, 0)

    # ── Build team & agent nodes ──────────────────────────────────────
    agents_seen: set[str] = set()
    total_messages = 0
    for t in teams:
        tid = str(t["_id"])
        cids = team_conv_ids.get(tid, [])
        team_msgs = sum(msg_counts.get(c, 0) for c in cids)
        total_messages += team_msgs
        team_cost = cost_by_team.get(tid, 0.0)
        team_time = time_by_team.get(tid, 0)
        team_calls = calls_by_team.get(tid, 0)
        team_node = {
            "id": f"team:{tid}",
            "label": t.get("name") or "Team",
            "kind": "team",
            "weight": 13 + min(8, team_msgs // 10),
            "cost": round(team_cost, 4),
            "time_ms": team_time,
            "calls": team_calls,
            "subtitle": f"{team_msgs} msgs · ${team_cost:.2f} · {team_calls} LLM calls",
            "conv_count": len(cids),
        }
        nodes.append(team_node)
        edges.append({"source": org_node["id"], "target": team_node["id"],
                      "weight": max(1, team_msgs), "kind": "team-of"})

        for aid in (t.get("agents") or []):
            n = agent_msg_counts.get(aid, 0)
            a_cost = cost_by_agent.get(aid, 0.0)
            a_time = time_by_agent.get(aid, 0)
            a_calls = calls_by_agent.get(aid, 0)
            if n == 0 and a_calls == 0 and aid in agents_seen:
                # Agent already added under another team and inactive here
                continue
            agents_seen.add(aid)
            agent_node = {
                "id": f"agent:{tid}:{aid}",
                "label": aid.replace("-", " ").title(),
                "kind": "agent",
                "weight": 6 + min(6, n // 4),
                "cost": round(a_cost, 4),
                "time_ms": a_time,
                "calls": a_calls,
                "subtitle": f"{n} msgs · ${a_cost:.2f}",
                "agent_id": aid,
            }
            nodes.append(agent_node)
            edges.append({"source": team_node["id"], "target": agent_node["id"],
                          "weight": max(1, n), "kind": "agent-of"})

    # ── Recent missions (last N) ──────────────────────────────────────
    miss_match: dict = {"org_id": org_id}
    if start is not None:
        miss_match["_id"] = {"$gte": ObjectId.from_datetime(start)}
    miss_cursor = db.missions.find(
        miss_match,
        {"goal": 1, "status": 1, "team_id": 1, "lead_agent_id": 1, "created_at": 1},
    ).sort("_id", -1).limit(40)
    missions = await miss_cursor.to_list(40)
    # Per-mission cost/time
    mission_costs: dict[str, dict] = {}
    if missions:
        mids = [str(m["_id"]) for m in missions]
        async for r in db.llm_calls.aggregate([
            {"$match": {**tele_match, "mission_id": {"$in": mids}}},
            {"$group": {
                "_id": "$mission_id",
                "cost": {"$sum": "$cost_usd"},
                "time_ms": {"$sum": "$latency_ms"},
                "calls": {"$sum": 1},
            }},
        ]):
            mission_costs[r["_id"]] = {
                "cost": float(r.get("cost") or 0.0),
                "time_ms": int(r.get("time_ms") or 0),
                "calls": int(r.get("calls") or 0),
            }

    for m in missions:
        mid = str(m["_id"])
        team_link = f"team:{m.get('team_id')}" if m.get("team_id") else org_node["id"]
        if not any(n["id"] == team_link for n in nodes):
            team_link = org_node["id"]
        mc = mission_costs.get(mid, {})
        m_node = {
            "id": f"mission:{mid}",
            "label": (m.get("goal") or "Mission")[:60],
            "kind": "mission",
            "status": m.get("status") or "unknown",
            "weight": 8,
            "cost": round(mc.get("cost", 0.0), 4),
            "time_ms": int(mc.get("time_ms", 0)),
            "calls": int(mc.get("calls", 0)),
            "subtitle": f"{m.get('status') or '?'} · ${mc.get('cost', 0.0):.2f}",
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
            "time_ms": 0,
            "calls": 0,
            "subtitle": f"{p.get('platform') or 'any'} · {p.get('status') or 'draft'}",
        }
        nodes.append(p_node)
        edges.append({"source": org_node["id"], "target": p_node["id"],
                      "weight": 1, "kind": "post-of"})

    # ── Active members (top spenders by LLM cost) ─────────────────────
    if cost_by_user:
        top_users = sorted(cost_by_user.items(), key=lambda x: x[1], reverse=True)[:10]
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
                "time_ms": int(time_by_user.get(uid, 0)),
                "calls": 0,
                "subtitle": f"${cost:.2f} · {label}",
            }
            nodes.append(u_node)
            edges.append({"source": org_node["id"], "target": u_node["id"],
                          "weight": max(1, int(cost * 100)),
                          "kind": "member-of"})

    # ── Heatmap normalisation maxes ───────────────────────────────────
    cost_max = max((n.get("cost") or 0.0) for n in nodes) if nodes else 0.0
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
            "llm_cost_usd": round(total_cost, 4),
            "llm_input_tokens": total_in,
            "llm_output_tokens": total_out,
            "llm_calls": total_calls,
            "llm_time_ms": total_time_ms,
            "cost_max": round(float(cost_max), 4),
            "time_ms_max": int(time_ms_max),
        },
        "nodes": nodes,
        "edges": edges,
    }
