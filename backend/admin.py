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

import bcrypt
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr


def _iso(d):
    if isinstance(d, datetime):
        return d.isoformat()
    return d


def _public_rates_table() -> list[dict]:
    """Return a flat, dashboard-friendly view of every external-API rate
    we know about. The frontend renders this as a "Pricing reference"
    table so admins can see exactly what each provider charges (and
    cross-check it against their own bill)."""
    try:
        from external_api_costs import EXTERNAL_API_RATES
    except Exception:
        return []
    rows = []
    for provider, rate in sorted(EXTERNAL_API_RATES.items()):
        # Render each rate in a single human-readable line.
        parts = []
        if "per_call" in rate:
            v = rate["per_call"]
            parts.append("free" if v == 0 else f"${v:.4g}/call")
        if "per_second" in rate:
            parts.append(f"${rate['per_second']:.4g}/sec")
        if "input" in rate:
            parts.append(f"${rate['input']:.4g}/1k input tok")
        if "output" in rate:
            parts.append(f"${rate['output']:.4g}/1k output tok")
        rows.append({
            "provider": provider,
            "rate_summary": " · ".join(parts) or "—",
        })
    return rows


def _hash_password(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()


class UserCreateBody(BaseModel):
    name: str
    email: EmailStr
    password: str
    is_superadmin: bool = False


class UserUpdateBody(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    is_superadmin: Optional[bool] = None
    is_disabled: Optional[bool] = None


def make_admin_router(*, db, get_current_user) -> APIRouter:
    r = APIRouter(prefix="/api/admin")

    async def require_superadmin(u: dict = Depends(get_current_user)) -> dict:
        if not u.get("is_superadmin"):
            raise HTTPException(403, "Superadmin only")
        return u

    # ── Overview ─────────────────────────────────────────────────────────
    @r.get("/overview")
    async def overview(
        _: dict = Depends(require_superadmin),
        exclude_test: bool = Query(False, description="Exclude obvious test accounts (emails matching @test.com / @example.com / @x.com / synthetic patterns)"),
    ):
        """Top-line counts + 30-day signup / activity sparklines.

        Field-name reality check (Feb 2026): collections do NOT all use
        ``created_at``. Today: messages → ``timestamp``, missions →
        ``started_at``, everything else → ``created_at``. We honour each
        collection's actual field below; using the wrong one returned
        zeroed deltas / DAU before this fix.

        ``exclude_test=true`` filters out our automated-test accounts so
        the totals reflect *real* humans on the platform. Heuristic is
        deliberately broad (catches @test.com, @example.com, @x.com, and
        emails with timestamp-style numeric suffixes) so the dashboard
        gives an honest picture of real adoption.
        """
        now = datetime.now(timezone.utc)
        d7 = now - timedelta(days=7)
        d30 = now - timedelta(days=30)

        # Build a "real users" filter from email heuristics. All test orgs
        # are owned by these test users, so by filtering both we strip the
        # whole testing footprint without touching real customer data.
        test_email_rx = r"@(test|example|t)\.com$|@x\.com$|@xx?\.com$|^(test_|test-|sr\d?-|cont-|cf-|v2-|gridfs-|jwt-|dbg-|nope-|cont-fixed-|soulreview-|owner-|loginfix-|about-link-)"
        real_user_filter = {} if not exclude_test else {"email": {"$not": {"$regex": test_email_rx, "$options": "i"}}}

        # If we're excluding tests, get the set of real user IDs once and
        # use it to scope the org / mission / message filters.
        real_user_ids: list[str] = []
        if exclude_test:
            cur = db.users.find(real_user_filter, {"_id": 1})
            real_user_ids = [str(u["_id"]) async for u in cur]

        org_filter = {} if not exclude_test else {"owner_id": {"$in": real_user_ids}}

        # Counts
        users_total = await db.users.count_documents(real_user_filter)
        orgs_total = await db.organizations.count_documents(org_filter)
        teams_total = await db.teams.count_documents({})  # teams are auto-created with orgs; same scaling applies
        missions_total = await db.missions.count_documents({})
        social_total = await db.social_posts.count_documents({})
        convos_total = await db.conversations.count_documents({})
        msgs_total = await db.messages.count_documents({})

        # 7d / 30d slices — match each collection's actual timestamp field.
        new_users_7d = await db.users.count_documents({**real_user_filter, "created_at": {"$gte": d7}})
        new_users_30d = await db.users.count_documents({**real_user_filter, "created_at": {"$gte": d30}})
        new_orgs_7d = await db.organizations.count_documents({**org_filter, "created_at": {"$gte": d7}})
        new_orgs_30d = await db.organizations.count_documents({**org_filter, "created_at": {"$gte": d30}})
        new_missions_7d = await db.missions.count_documents({"started_at": {"$gte": d7}})
        new_missions_30d = await db.missions.count_documents({"started_at": {"$gte": d30}})

        # Daily signup chart (last 30 days)
        sign_pipeline = [
            {"$match": {**real_user_filter, "created_at": {"$gte": d30}}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
                "count": {"$sum": 1},
            }},
            {"$sort": {"_id": 1}},
        ]
        signup_series = [{"date": x["_id"], "count": x["count"]} async for x in db.users.aggregate(sign_pipeline)]

        # Active users — DAU/WAU/MAU based on messages.timestamp.
        # Distinct over user-typed messages, dropping any null senders.
        async def _active(since):
            q: dict = {"sender_type": "user", "timestamp": {"$gte": since}}
            if exclude_test and real_user_ids:
                q["sender_id"] = {"$in": real_user_ids}
            ids = await db.messages.distinct("sender_id", q)
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

        # Top users by spend — merges LLM + external API spend per
        # user. Anonymous calls (no user_id) are dropped so the
        # leaderboard reflects real humans, not background jobs. Both
        # collections key user attribution under `user_id`, but the
        # llm_calls collection stores it via the telemetry context
        # wrapper while external_api_calls stores it via the ctx
        # argument; the shape is identical at query time.
        llm_user_pipe = [
            {"$match": {"created_at": {"$gte": since},
                        "user_id": {"$nin": [None, ""]}}},
            {"$group": {
                "_id": "$user_id",
                "llm_calls": {"$sum": 1},
                "tokens": {"$sum": "$total_tokens"},
                "llm_cost": {"$sum": "$cost_usd"},
            }},
        ]
        ext_user_pipe = [
            {"$match": {"ts": {"$gte": since},
                        "user_id": {"$nin": [None, ""]}}},
            {"$group": {
                "_id": "$user_id",
                "ext_calls": {"$sum": "$calls"},
                "ext_cost":  {"$sum": "$cost_usd"},
            }},
        ]
        # Merge in Python — both aggs are tiny (≤thousands of distinct
        # users) so a join in code is cheaper than $unionWith ceremony.
        by_user: dict[str, dict] = {}
        async for row in db.llm_calls.aggregate(llm_user_pipe):
            uid = row["_id"]
            by_user[uid] = {
                "user_id": uid,
                "llm_calls": row.get("llm_calls", 0),
                "tokens": row.get("tokens", 0) or 0,
                "llm_cost": round(row.get("llm_cost", 0) or 0, 4),
                "ext_calls": 0,
                "ext_cost": 0.0,
            }
        async for row in db.external_api_calls.aggregate(ext_user_pipe):
            uid = row["_id"]
            slot = by_user.setdefault(uid, {
                "user_id": uid,
                "llm_calls": 0, "tokens": 0, "llm_cost": 0.0,
                "ext_calls": 0, "ext_cost": 0.0,
            })
            slot["ext_calls"] = row.get("ext_calls", 0)
            slot["ext_cost"] = round(row.get("ext_cost", 0) or 0, 4)
        # Compute total cost per user, sort, hydrate emails for top 20.
        for v in by_user.values():
            v["total_cost"] = round(v["llm_cost"] + v["ext_cost"], 4)
            v["total_calls"] = v["llm_calls"] + v["ext_calls"]
        top_users_rows = sorted(
            by_user.values(), key=lambda r: r["total_cost"], reverse=True,
        )[:20]
        for row in top_users_rows:
            try:
                u = await db.users.find_one(
                    {"_id": ObjectId(row["user_id"])},
                    {"email": 1, "name": 1},
                )
            except Exception:
                u = None
            row["user_email"] = (u or {}).get("email") or "(deleted)"
            row["user_name"] = (u or {}).get("name") or ""

        # ── External APIs (Brave / Perplexity / Resend / kie.ai) ─────
        # Same time window, separate collection, aggregated three ways
        # so the dashboard can render per-provider, per-kind, and a
        # combined grand total alongside LLM spend.
        ext_total_pipe = [
            {"$match": {"ts": {"$gte": since}}},
            {"$group": {
                "_id": None,
                "calls": {"$sum": "$calls"},
                "ok":    {"$sum": {"$cond": ["$success", 1, 0]}},
                "cost":  {"$sum": "$cost_usd"},
            }},
        ]
        ext_total_agg = [x async for x in db.external_api_calls.aggregate(ext_total_pipe)]
        ext_totals = (
            ext_total_agg[0] if ext_total_agg
            else {"calls": 0, "ok": 0, "cost": 0}
        )
        ext_totals.pop("_id", None)
        ext_totals["cost"] = round(ext_totals.get("cost", 0) or 0, 4)

        ext_by_provider_pipe = [
            {"$match": {"ts": {"$gte": since}}},
            {"$group": {
                "_id": "$provider",
                "calls": {"$sum": "$calls"},
                "cost":  {"$sum": "$cost_usd"},
            }},
            {"$sort": {"cost": -1}},
        ]
        ext_by_provider = [
            {"provider": x["_id"] or "unknown",
             "calls": x["calls"], "cost": round(x["cost"] or 0, 4)}
            async for x in db.external_api_calls.aggregate(ext_by_provider_pipe)
        ]

        ext_daily_pipe = [
            {"$match": {"ts": {"$gte": since}}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$ts"}},
                "calls": {"$sum": "$calls"},
                "cost":  {"$sum": "$cost_usd"},
            }},
            {"$sort": {"_id": 1}},
        ]
        ext_daily_series = [
            {"date": x["_id"], "calls": x["calls"], "cost": round(x["cost"] or 0, 4)}
            async for x in db.external_api_calls.aggregate(ext_daily_pipe)
        ]

        return {
            "since": _iso(since),
            "totals": totals,
            "by_model": by_model,
            "by_kind": by_kind,
            "daily_series": daily_series,
            "top_orgs": top_orgs,
            # Per-user spend leaderboard. Merges LLM + external API
            # rows so admins can answer "who is burning the budget?".
            "top_users": top_users_rows,
            # New: non-LLM third-party API spend (Brave / Perplexity /
            # Resend / kie.ai). Frontend renders these in their own
            # cards next to the LLM cards.
            "external": {
                "totals": ext_totals,
                "by_provider": ext_by_provider,
                "daily_series": ext_daily_series,
                "rates": _public_rates_table(),
            },
            # Combined grand total — LLM + external — so users have a
            # single "what did this cost me" number.
            "grand_total_cost": round(
                (totals.get("cost") or 0) + (ext_totals.get("cost") or 0), 4
            ),
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
            # Messages collection uses ``timestamp`` (not ``created_at``) —
            # querying the wrong field made everyone look inactive.
            last_msg = await db.messages.find_one({"sender_type": "user", "sender_id": uid},
                                                    sort=[("timestamp", -1)],
                                                    projection={"timestamp": 1})
            rows.append({
                "id": uid,
                "email": u.get("email") or "",
                "name": u.get("name") or "",
                "is_superadmin": bool(u.get("is_superadmin")),
                "firebase_uid": u.get("firebase_uid") or "",
                "firebase_provider": u.get("firebase_provider") or "",
                "is_disabled": bool(u.get("is_disabled")),
                "created_at": _iso(u.get("created_at")),
                "last_active_at": _iso((last_msg or {}).get("timestamp")),
                "org_count": org_count,
            })
        return {"page": page, "page_size": page_size, "total": total, "rows": rows}

    # ── User CRUD (admin add / edit / delete) ────────────────────────────

    @r.post("/users")
    async def create_user(body: UserCreateBody, _: dict = Depends(require_superadmin)):
        """Create a fresh Mongo user record. Password is bcrypt-hashed
        for the legacy email/password path — Firebase users will get
        their UID stamped on this record the first time they sign in
        with the same email (handled by the `upsert_user_for_firebase_claims`
        link-by-email rule in `firebase_auth.py`)."""
        email = body.email.lower().strip()
        if not email:
            raise HTTPException(400, "Email is required")
        if len(body.password) < 6:
            raise HTTPException(400, "Password must be at least 6 characters")
        existing = await db.users.find_one({"email": email})
        if existing:
            raise HTTPException(409, "A user with this email already exists")
        now = datetime.now(timezone.utc)
        doc = {
            "name": (body.name or email.split("@")[0]).strip(),
            "email": email,
            "password_hash": _hash_password(body.password),
            "is_superadmin": bool(body.is_superadmin),
            "created_at": now,
        }
        res = await db.users.insert_one(doc)
        return {
            "id": str(res.inserted_id),
            "email": doc["email"],
            "name": doc["name"],
            "is_superadmin": doc["is_superadmin"],
            "created_at": _iso(now),
        }

    @r.patch("/users/{user_id}")
    async def update_user(
        user_id: str, body: UserUpdateBody, admin: dict = Depends(require_superadmin),
    ):
        """Edit a user. Any combination of fields is allowed; omitted
        fields are left untouched. An admin can NOT remove their own
        superadmin flag — must be done by another admin."""
        try:
            target = await db.users.find_one({"_id": ObjectId(user_id)})
        except Exception:
            raise HTTPException(400, "Invalid user id")
        if not target:
            raise HTTPException(404, "User not found")

        update: dict = {}
        if body.name is not None and body.name.strip():
            update["name"] = body.name.strip()
        if body.email is not None:
            new_email = body.email.lower().strip()
            if new_email != target.get("email"):
                clash = await db.users.find_one({"email": new_email, "_id": {"$ne": target["_id"]}})
                if clash:
                    raise HTTPException(409, "Another user already uses that email")
                update["email"] = new_email
        if body.password is not None:
            if len(body.password) < 6:
                raise HTTPException(400, "Password must be at least 6 characters")
            update["password_hash"] = _hash_password(body.password)
        if body.is_superadmin is not None:
            if str(target["_id"]) == str(admin["id"]) and body.is_superadmin is False:
                raise HTTPException(400, "Admins cannot remove their own superadmin flag — ask another admin")
            update["is_superadmin"] = bool(body.is_superadmin)
        if body.is_disabled is not None:
            if str(target["_id"]) == str(admin["id"]) and body.is_disabled is True:
                raise HTTPException(400, "You cannot disable your own account")
            update["is_disabled"] = bool(body.is_disabled)

        if not update:
            raise HTTPException(400, "No fields to update")
        await db.users.update_one({"_id": target["_id"]}, {"$set": update})
        fresh = await db.users.find_one({"_id": target["_id"]}, {"password_hash": 0})
        return {
            "id": str(fresh["_id"]),
            "email": fresh.get("email") or "",
            "name": fresh.get("name") or "",
            "is_superadmin": bool(fresh.get("is_superadmin")),
            "is_disabled": bool(fresh.get("is_disabled")),
        }

    @r.delete("/users/{user_id}")
    async def delete_user(
        user_id: str, admin: dict = Depends(require_superadmin),
    ):
        """Permanently remove a user record + all org memberships they
        held. Conversations / messages they posted are preserved
        (anonymised by the missing user record, since the rest of the
        system identifies them by id only). Refuses to delete the
        currently-signed-in admin so they don't lock themselves out."""
        try:
            target = await db.users.find_one({"_id": ObjectId(user_id)})
        except Exception:
            raise HTTPException(400, "Invalid user id")
        if not target:
            raise HTTPException(404, "User not found")
        if str(target["_id"]) == str(admin["id"]):
            raise HTTPException(400, "You cannot delete your own account")
        # Cascade: drop org memberships so the user vanishes from member lists.
        await db.org_members.delete_many({"user_id": str(target["_id"])})
        await db.users.delete_one({"_id": target["_id"]})
        return {"ok": True, "deleted_id": str(target["_id"])}

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
        # Missions store the start time as ``started_at`` (no ``created_at``
        # field exists). Sorting by the wrong key returned an arbitrary
        # order rather than newest-first.
        cur = db.missions.find(q).sort("started_at", -1).limit(limit)
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
                "created_at": _iso(m.get("started_at")),
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
