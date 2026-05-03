"""Admin router — superadmin-only endpoints powering the /admin dashboard.

Auth: every route is gated by ``require_superadmin`` which 403s any user
who doesn't have ``is_superadmin: true`` on their user row.

Seed flow (server.py boot): ``ensure_superadmin_seed()`` finds the
configured superadmin email and stamps the flag on that user row if it
exists. New superadmins can be added by setting the flag manually in
Mongo or by extending the seed list.

All endpoints respect the read-only nature of an analytics dashboard —
nothing here mutates org/user/mission data. The single write surface
is ``llm_calls`` insertions, but those happen in ``llm_logging.py``,
not here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query


def _iso(d):
    if isinstance(d, datetime):
        return d.isoformat()
    return d


def make_admin_router(*, db, get_current_user) -> APIRouter:
    r = APIRouter(prefix="/api/admin")

    async def require_superadmin(u: dict = Depends(get_current_user)) -> dict:
        if not u.get("is_superadmin"):
            raise HTTPException(403, "Superadmin only")
        return u

    # ── Overview ─────────────────────────────────────────────────────────
    @r.get("/overview")
    async def overview(_: dict = Depends(require_superadmin)):
        """Top-line counts + 30-day signup / activity sparklines."""
        now = datetime.now(timezone.utc)
        d7 = now - timedelta(days=7)
        d30 = now - timedelta(days=30)

        # Counts
        users_total = await db.users.count_documents({})
        orgs_total = await db.organizations.count_documents({})
        teams_total = await db.teams.count_documents({})
        missions_total = await db.missions.count_documents({})
        social_total = await db.social_posts.count_documents({})
        convos_total = await db.conversations.count_documents({})
        msgs_total = await db.messages.count_documents({})

        # 7d / 30d slices
        new_users_7d = await db.users.count_documents({"created_at": {"$gte": d7}})
        new_users_30d = await db.users.count_documents({"created_at": {"$gte": d30}})
        new_orgs_7d = await db.organizations.count_documents({"created_at": {"$gte": d7}})
        new_orgs_30d = await db.organizations.count_documents({"created_at": {"$gte": d30}})
        new_missions_7d = await db.missions.count_documents({"created_at": {"$gte": d7}})
        new_missions_30d = await db.missions.count_documents({"created_at": {"$gte": d30}})

        # Daily signup chart (last 30 days)
        sign_pipeline = [
            {"$match": {"created_at": {"$gte": d30}}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
                "count": {"$sum": 1},
            }},
            {"$sort": {"_id": 1}},
        ]
        signup_series = [{"date": x["_id"], "count": x["count"]} async for x in db.users.aggregate(sign_pipeline)]

        # Active users — DAU/WAU/MAU based on messages.created_at
        # (mongo unique by sender_id which is the user id for user-typed messages).
        async def _active(since):
            ids = await db.messages.distinct("sender_id", {"sender_type": "user", "created_at": {"$gte": since}})
            return len([i for i in ids if i])
        dau = await _active(now - timedelta(days=1))
        wau = await _active(d7)
        mau = await _active(d30)

        # Mission status breakdown (lifetime)
        mission_status_agg = [
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        ]
        mission_status = {x["_id"] or "unknown": x["count"] async for x in db.missions.aggregate(mission_status_agg)}

        return {
            "totals": {
                "users": users_total,
                "orgs": orgs_total,
                "teams": teams_total,
                "missions": missions_total,
                "social_posts": social_total,
                "conversations": convos_total,
                "messages": msgs_total,
            },
            "deltas": {
                "new_users_7d": new_users_7d,
                "new_users_30d": new_users_30d,
                "new_orgs_7d": new_orgs_7d,
                "new_orgs_30d": new_orgs_30d,
                "new_missions_7d": new_missions_7d,
                "new_missions_30d": new_missions_30d,
            },
            "active": {"dau": dau, "wau": wau, "mau": mau},
            "signup_series": signup_series,
            "mission_status": mission_status,
        }

    # ── LLM usage ────────────────────────────────────────────────────────
    @r.get("/llm-usage")
    async def llm_usage(
        _: dict = Depends(require_superadmin),
        days: int = Query(30, ge=1, le=365),
    ):
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days)

        total_pipe = [
            {"$match": {"created_at": {"$gte": since}}},
            {"$group": {
                "_id": None,
                "calls": {"$sum": 1},
                "ok": {"$sum": {"$cond": ["$ok", 1, 0]}},
                "tokens": {"$sum": "$total_tokens"},
                "cost": {"$sum": "$cost_usd"},
            }},
        ]
        agg = [x async for x in db.llm_calls.aggregate(total_pipe)]
        totals = (agg[0] if agg else {"calls": 0, "ok": 0, "tokens": 0, "cost": 0})
        totals.pop("_id", None)
        totals["cost"] = round(totals.get("cost", 0) or 0, 4)

        # By model
        by_model_pipe = [
            {"$match": {"created_at": {"$gte": since}}},
            {"$group": {
                "_id": "$model",
                "calls": {"$sum": 1},
                "tokens": {"$sum": "$total_tokens"},
                "cost": {"$sum": "$cost_usd"},
            }},
            {"$sort": {"cost": -1}},
            {"$limit": 25},
        ]
        by_model = [{"model": x["_id"] or "unknown",
                     "calls": x["calls"],
                     "tokens": x["tokens"] or 0,
                     "cost": round(x["cost"] or 0, 4)}
                    async for x in db.llm_calls.aggregate(by_model_pipe)]

        # By kind_label
        by_kind_pipe = [
            {"$match": {"created_at": {"$gte": since}}},
            {"$group": {"_id": "$kind_label", "calls": {"$sum": 1}, "cost": {"$sum": "$cost_usd"}}},
            {"$sort": {"cost": -1}},
        ]
        by_kind = [{"kind": x["_id"] or "uncategorised",
                    "calls": x["calls"],
                    "cost": round(x["cost"] or 0, 4)}
                   async for x in db.llm_calls.aggregate(by_kind_pipe)]

        # Daily cost time series
        daily_pipe = [
            {"$match": {"created_at": {"$gte": since}}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
                "calls": {"$sum": 1},
                "cost": {"$sum": "$cost_usd"},
            }},
            {"$sort": {"_id": 1}},
        ]
        daily_series = [{"date": x["_id"],
                         "calls": x["calls"],
                         "cost": round(x["cost"] or 0, 4)}
                        async for x in db.llm_calls.aggregate(daily_pipe)]

        # Top orgs by spend
        top_orgs_pipe = [
            {"$match": {"created_at": {"$gte": since}, "org_id": {"$ne": None}}},
            {"$group": {
                "_id": "$org_id",
                "calls": {"$sum": 1},
                "tokens": {"$sum": "$total_tokens"},
                "cost": {"$sum": "$cost_usd"},
            }},
            {"$sort": {"cost": -1}},
            {"$limit": 15},
        ]
        top_orgs_raw = [x async for x in db.llm_calls.aggregate(top_orgs_pipe)]
        top_orgs = []
        for row in top_orgs_raw:
            org_id = row["_id"]
            try:
                org = await db.organizations.find_one({"_id": ObjectId(org_id)}, {"name": 1})
            except Exception:
                org = None
            top_orgs.append({
                "org_id": org_id,
                "org_name": (org or {}).get("name") or "(deleted)",
                "calls": row["calls"],
                "tokens": row["tokens"] or 0,
                "cost": round(row["cost"] or 0, 4),
            })

        return {
            "since": _iso(since),
            "totals": totals,
            "by_model": by_model,
            "by_kind": by_kind,
            "daily_series": daily_series,
            "top_orgs": top_orgs,
        }

    # ── Orgs table ───────────────────────────────────────────────────────
    @r.get("/orgs")
    async def list_orgs(
        _: dict = Depends(require_superadmin),
        page: int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=100),
        search: str = Query(""),
    ):
        q: dict = {}
        if search.strip():
            q["name"] = {"$regex": search.strip(), "$options": "i"}
        total = await db.organizations.count_documents(q)
        cur = db.organizations.find(q).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size)
        rows = []
        async for o in cur:
            org_id = str(o["_id"])
            owner_id = o.get("owner_id") or ""
            owner = None
            if owner_id:
                try:
                    owner = await db.users.find_one({"_id": ObjectId(owner_id)}, {"email": 1, "name": 1})
                except Exception:
                    owner = None
            members = await db.org_members.count_documents({"org_id": org_id})
            missions = await db.missions.count_documents({"org_id": org_id})
            posts = await db.social_posts.count_documents({"org_id": org_id})
            spend_pipe = [
                {"$match": {"org_id": org_id}},
                {"$group": {"_id": None, "calls": {"$sum": 1}, "cost": {"$sum": "$cost_usd"}}},
            ]
            spend = [x async for x in db.llm_calls.aggregate(spend_pipe)]
            spend_doc = spend[0] if spend else {"calls": 0, "cost": 0}
            rows.append({
                "id": org_id,
                "name": o.get("name") or "",
                "owner_email": (owner or {}).get("email") or "",
                "owner_name": (owner or {}).get("name") or "",
                "members": members,
                "missions": missions,
                "social_posts": posts,
                "llm_calls": spend_doc.get("calls", 0),
                "llm_cost": round(spend_doc.get("cost", 0) or 0, 4),
                "created_at": _iso(o.get("created_at")),
            })
        return {"page": page, "page_size": page_size, "total": total, "rows": rows}

    # ── Users table ──────────────────────────────────────────────────────
    @r.get("/users")
    async def list_users(
        _: dict = Depends(require_superadmin),
        page: int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=100),
        search: str = Query(""),
    ):
        q: dict = {}
        if search.strip():
            term = search.strip()
            q["$or"] = [
                {"email": {"$regex": term, "$options": "i"}},
                {"name":  {"$regex": term, "$options": "i"}},
            ]
        total = await db.users.count_documents(q)
        cur = db.users.find(q, {"password_hash": 0}).sort("created_at", -1).skip((page - 1) * page_size).limit(page_size)
        rows = []
        async for u in cur:
            uid = str(u["_id"])
            org_count = await db.org_members.count_documents({"user_id": uid})
            last_msg = await db.messages.find_one({"sender_type": "user", "sender_id": uid},
                                                    sort=[("created_at", -1)],
                                                    projection={"created_at": 1})
            rows.append({
                "id": uid,
                "email": u.get("email") or "",
                "name": u.get("name") or "",
                "is_superadmin": bool(u.get("is_superadmin")),
                "created_at": _iso(u.get("created_at")),
                "last_active_at": _iso((last_msg or {}).get("created_at")),
                "org_count": org_count,
            })
        return {"page": page, "page_size": page_size, "total": total, "rows": rows}

    # ── Recent missions ──────────────────────────────────────────────────
    @r.get("/missions")
    async def recent_missions(
        _: dict = Depends(require_superadmin),
        limit: int = Query(50, ge=1, le=200),
        status: Optional[str] = None,
    ):
        q: dict = {}
        if status:
            q["status"] = status
        cur = db.missions.find(q).sort("created_at", -1).limit(limit)
        rows = []
        async for m in cur:
            org = None
            try:
                org = await db.organizations.find_one({"_id": ObjectId(m.get("org_id", ""))}, {"name": 1})
            except Exception:
                org = None
            rows.append({
                "id": m.get("id") or str(m.get("_id", "")),
                "goal": (m.get("goal") or "")[:200],
                "status": m.get("status"),
                "org_id": m.get("org_id"),
                "org_name": (org or {}).get("name") or "",
                "lead_agent_name": m.get("lead_agent_name"),
                "iteration_count": m.get("iteration_count", 0),
                "created_at": _iso(m.get("created_at")),
                "finished_at": _iso(m.get("finished_at")),
            })
        return {"rows": rows}

    return r


async def ensure_superadmin_seed(db, emails: list[str]) -> None:
    """Stamp ``is_superadmin: true`` on the configured user rows if they
    exist. Idempotent — running every boot is safe. Users that haven't
    registered yet will get the flag the next time someone runs this
    after they sign up (e.g. on the next backend restart).
    """
    for email in emails:
        try:
            await db.users.update_one(
                {"email": email.lower().strip()},
                {"$set": {"is_superadmin": True}},
            )
        except Exception:
            pass
