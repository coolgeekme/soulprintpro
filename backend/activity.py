"""Org activity dashboard.

A single endpoint that aggregates KPIs + a unified, time-ordered activity
feed across all the moving parts of an organization:

- Social posts (drafts / queued / published)
- Schedules (created / fired / paused)
- Conversations + their latest agent reply
- Asset library uploads

Designed for the org "Overview" tab — KPI tiles up top, feed below.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)


def _iso(v) -> Optional[str]:
    if isinstance(v, datetime):
        return v.isoformat()
    return v or None


def make_activity_router(*, db, require_org_member, get_current_user, AGENTS):
    r = APIRouter(prefix="/api")

    @r.get("/orgs/{org_id}/activity")
    async def org_activity(
        org_id: str, limit: int = 50, u: dict = Depends(get_current_user),
    ):
        await require_org_member(org_id, u["id"])
        limit = max(1, min(200, limit))
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)

        # ── KPIs ───────────────────────────────────────────────────────
        social_drafts = await db.social_posts.count_documents({"org_id": org_id, "status": "draft"})
        social_queued = await db.social_posts.count_documents({"org_id": org_id, "status": "queued"})
        social_published = await db.social_posts.count_documents({"org_id": org_id, "status": "published"})
        social_published_7d = await db.social_posts.count_documents(
            {"org_id": org_id, "status": "published", "published_at": {"$gte": week_ago}}
        )

        active_schedules = await db.schedules.count_documents({"org_id": org_id, "active": True})
        total_schedules = await db.schedules.count_documents({"org_id": org_id})

        # Resolve teams + conversations under this org
        team_docs = await db.teams.find({"org_id": org_id}, {"_id": 1, "name": 1, "is_general": 1, "is_direct": 1}).to_list(500)
        team_ids = [str(t["_id"]) for t in team_docs]
        team_lookup = {str(t["_id"]): t for t in team_docs}
        total_teams = len(team_ids)
        conversations = []
        if team_ids:
            conv_cur = db.conversations.find({"team_id": {"$in": team_ids}}).sort("created_at", -1)
            conversations = await conv_cur.to_list(500)
        total_conversations = len(conversations)
        active_convs_7d = sum(
            1 for c in conversations if isinstance(c.get("last_message_at"), datetime) and c["last_message_at"] >= week_ago
        )

        total_assets = await db.assets.count_documents({"org_id": org_id})

        # ── Activity feed ───────────────────────────────────────────────
        # Each source contributes a list; we merge + sort by timestamp desc
        # and trim to `limit`. Schemas below stay JSON-friendly (ISO strings).
        feed: list[dict] = []

        # 1) Social posts — surface creates + status changes
        soc_cur = db.social_posts.find({"org_id": org_id}).sort("created_at", -1).limit(limit)
        async for p in soc_cur:
            kind = "social.draft" if p.get("status") == "draft" else (
                "social.scheduled" if p.get("status") == "queued" else (
                "social.published" if p.get("status") == "published" else "social.draft"))
            feed.append({
                "kind": kind,
                "title": (p.get("text") or "")[:140],
                "subtitle": ", ".join((p.get("platforms") or [])[:3]) or "social",
                "agent_name": p.get("agent_name") or "",
                "timestamp": _iso(p.get("published_at") or p.get("scheduled_for") or p.get("created_at")),
                "deeplink": f"/app/org/{org_id}/social",
                "image_url": ((p.get("image") or {}) or {}).get("url"),
            })

        # 2) Schedules — most-recent runs (last_run_at) AND creation
        sched_cur = db.schedules.find({"org_id": org_id}).sort("created_at", -1).limit(limit)
        async for s in sched_cur:
            tgt_team = ""
            if s.get("target_id") and s.get("scope") == "conversation":
                # best-effort: find the team the conv lives under
                conv_doc = next((c for c in conversations if str(c["_id"]) == s["target_id"]), None)
                if conv_doc:
                    tgt_team = (team_lookup.get(conv_doc.get("team_id", "")) or {}).get("name", "")
            feed.append({
                "kind": "schedule.created",
                "title": s.get("title", "Schedule"),
                "subtitle": (s.get("cron") or ("once · " + (_iso(s.get("run_at")) or ""))) or "",
                "agent_name": "",
                "timestamp": _iso(s.get("created_at")),
                "deeplink": f"/app/org/{org_id}",
                "extra": {"active": bool(s.get("active")), "target": tgt_team},
            })
            if s.get("last_run_at"):
                feed.append({
                    "kind": "schedule.fired",
                    "title": s.get("title", "Schedule") + " ran",
                    "subtitle": s.get("last_status") or "ok",
                    "agent_name": "",
                    "timestamp": _iso(s.get("last_run_at")),
                    "deeplink": f"/app/org/{org_id}",
                })

        # 3) Conversations — list with their latest agent reply preview
        for c in conversations[:limit]:
            t = team_lookup.get(c.get("team_id", "")) or {}
            feed.append({
                "kind": "conversation.created",
                "title": c.get("title") or "New conversation",
                "subtitle": t.get("name", ""),
                "agent_name": "",
                "timestamp": _iso(c.get("created_at")),
                "deeplink": f"/app/team/{c.get('team_id', '')}/c/{str(c['_id'])}",
            })
            if isinstance(c.get("last_message_at"), datetime):
                # cheap latest-msg preview without an extra fetch — pull only
                # if it differs meaningfully from created_at
                last_msg = await db.messages.find_one(
                    {"conversation_id": str(c["_id"])},
                    sort=[("timestamp", -1)],
                )
                if last_msg and last_msg.get("sender_type") == "agent":
                    aid = last_msg.get("agent_id") or ""
                    feed.append({
                        "kind": "message.agent_reply",
                        "title": (last_msg.get("text") or "")[:140],
                        "subtitle": (c.get("title") or "Conversation"),
                        "agent_name": (AGENTS.get(aid) or {}).get("name", "Agent"),
                        "timestamp": _iso(last_msg.get("timestamp")),
                        "deeplink": f"/app/team/{c.get('team_id', '')}/c/{str(c['_id'])}",
                    })

        # 4) Asset uploads
        asset_cur = db.assets.find({"org_id": org_id}).sort("uploaded_at", -1).limit(limit)
        async for a in asset_cur:
            feed.append({
                "kind": "asset.uploaded",
                "title": a.get("original_name") or a.get("filename") or "Asset",
                "subtitle": "Asset library",
                "agent_name": "",
                "timestamp": _iso(a.get("uploaded_at")),
                "deeplink": f"/app/org/{org_id}",
            })

        # Sort merged feed by timestamp desc, drop entries with no timestamp
        feed = [f for f in feed if f.get("timestamp")]
        feed.sort(key=lambda f: f["timestamp"], reverse=True)
        feed = feed[:limit]

        return {
            "kpis": {
                "social_drafts": social_drafts,
                "social_queued": social_queued,
                "social_published": social_published,
                "social_published_7d": social_published_7d,
                "active_schedules": active_schedules,
                "total_schedules": total_schedules,
                "total_teams": total_teams,
                "total_conversations": total_conversations,
                "active_conversations_7d": active_convs_7d,
                "total_assets": total_assets,
            },
            "feed": feed,
        }

    @r.get("/orgs/{org_id}/recent-conversations")
    async def recent_conversations(
        org_id: str, limit: int = 5, u: dict = Depends(get_current_user),
    ):
        """Lightweight feed for the Org page's "pick up where you left off"
        strip. Returns the N most-recently-active conversations the user is
        a member of, each with a one-line preview of the latest message."""
        await require_org_member(org_id, u["id"])
        limit = max(1, min(20, limit))

        # Org owner can delete any conversation; members only their own.
        # The org's always-on Chief of Staff thread (general_conversation_id)
        # is auto-created on org provision and is never deletable from the
        # recent strip — the Org page links directly to it as the default
        # workspace.
        org = await db.organizations.find_one(
            {"_id": ObjectId(org_id)}, {"owner_id": 1, "general_conversation_id": 1}
        )
        is_owner = bool(org and str(org.get("owner_id")) == str(u["id"]))
        protected_conv_id = str((org or {}).get("general_conversation_id") or "")

        team_docs = await db.teams.find(
            {"org_id": org_id},
            {"_id": 1, "name": 1, "is_general": 1, "is_direct": 1},
        ).to_list(500)
        team_lookup = {str(t["_id"]): t for t in team_docs}
        team_ids = list(team_lookup.keys())
        if not team_ids:
            return []

        # Member-gated: only include conversations the caller is a member
        # of (direct chats are per-user, groups include the creator's id).
        conv_cur = db.conversations.find(
            {"team_id": {"$in": team_ids}, "member_ids": u["id"]},
        ).sort([("last_message_at", -1), ("created_at", -1)]).limit(limit * 2)
        convs = await conv_cur.to_list(limit * 2)

        out = []
        for c in convs[:limit]:
            t = team_lookup.get(c.get("team_id", "")) or {}
            last_msg = await db.messages.find_one(
                {"conversation_id": str(c["_id"])},
                sort=[("timestamp", -1)],
            )
            preview = ""
            agent_name = ""
            if last_msg:
                preview = (last_msg.get("text") or "").strip()[:160]
                if last_msg.get("sender_type") == "agent":
                    aid = last_msg.get("agent_id") or ""
                    agent_name = (AGENTS.get(aid) or {}).get("name", "Agent")
            ts = c.get("last_message_at") or c.get("created_at")
            is_creator = c.get("created_by") == u["id"]
            # Protect only the ALWAYS-ON Chief of Staff thread on this org
            # (the auto-created default workspace). Other conversations that
            # happen to live in the same General team — e.g. ones spawned
            # via smart-start — are deletable by their creator or the owner.
            is_general = bool(t.get("is_general"))
            is_protected = (str(c["_id"]) == protected_conv_id) if protected_conv_id else False
            out.append({
                "id": str(c["_id"]),
                "title": c.get("title") or "Conversation",
                "team_id": c.get("team_id", ""),
                "team_name": t.get("name", ""),
                "is_general": is_general,
                "is_direct": bool(t.get("is_direct")) or c.get("type") == "direct",
                "last_message_at": _iso(ts),
                "preview": preview,
                "agent_name": agent_name,
                "deeplink": f"/app/team/{c.get('team_id', '')}/c/{str(c['_id'])}",
                "can_delete": (is_creator or is_owner) and not is_protected,
            })
        return out

    return r
