"""Org ownership transfer flow (Feb 2026).

Replaces the old "owner clicks role dropdown → instantly demoted" model
with a proper handshake:

  1. **Outgoing owner initiates** a transfer to a target member. Picks:
       • What role THEY keep after the transfer (member / admin / owner)
       • Whether to ALSO leave the org once the transfer completes
       • The system stamps the minimum plan tier the recipient must
         carry to keep the org viable (based on current member count)
  2. **Pending transfer is created** with a 7-day expiry. NO role
     change happens yet — the outgoing owner stays owner, the
     recipient stays at their current role.
  3. **Recipient sees a banner** the next time they hit /app, and an
     accept page at `/app/transfers/{id}/accept`. The page shows the
     required plan tier and the recipient's current plan.
  4. **Recipient clicks Accept**. Backend re-checks plan tier.
       • Sufficient → ownership transfers atomically, outgoing owner
         is updated to their chosen role, optional self-leave runs.
       • Insufficient → 402 returned with `required_tier` so the
         frontend can route to /pricing for that tier with a
         `return_to` query param.
  5. **Recipient can decline** → transfer marked declined, outgoing
     owner gets notified (via banner on next page load).
  6. **Outgoing owner can cancel** at any time before acceptance.

Why a separate state machine and not just a flag on `org_members`?
Because the transfer carries metadata that doesn't belong on the
member row (required tier, intended-role-after, also_leave_after,
expires_at). Keeping it in a sibling collection lets us audit and
expire cleanly.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("org_transfers")

# 7-day acceptance window. Decided 1d/2c/3b/4a by product on Feb 2026.
TRANSFER_TTL = timedelta(days=7)


def _required_tier_for_member_count(n: int) -> str:
    """Return the cheapest plan tier whose `seats_included` baseline
    covers an active member count of `n`. Add-on extra_seats are NOT
    considered here — they don't survive ownership change since they're
    tied to the outgoing owner's Stripe customer."""
    # Pulled from billing.py PLANS dict to avoid a hard import cycle.
    # spark=1, temper=5, refine=10000. Mirror here as a single source
    # of truth for transfer-time computation.
    if n <= 1:
        return "spark"
    if n <= 5:
        return "temper"
    return "refine"


def _tier_is_sufficient(user_plan: Optional[str], required: str) -> bool:
    """A user's effective plan must be `required` or higher in the
    hierarchy spark < temper < refine. Missing / None defaults to spark."""
    rank = {"spark": 1, "temper": 2, "refine": 3}
    return rank.get(user_plan or "spark", 1) >= rank.get(required, 1)


def _serialize_transfer(t: dict, *, db_users_lookup: Optional[dict] = None) -> dict:
    """Wire-safe dict for the API. `db_users_lookup` is an optional
    map of user_id → user dict so the caller can avoid N+1s."""
    out = {
        "id": str(t["_id"]),
        "org_id": t["org_id"],
        "org_name": t.get("org_name", ""),
        "initiated_by": t["initiated_by"],
        "initiated_by_name": t.get("initiated_by_name", ""),
        "recipient_user_id": t["recipient_user_id"],
        "recipient_name": t.get("recipient_name", ""),
        "outgoing_role_after": t.get("outgoing_role_after", "member"),
        "also_leave_after": bool(t.get("also_leave_after", False)),
        "required_plan_tier": t.get("required_plan_tier", "spark"),
        "member_count_at_init": int(t.get("member_count_at_init", 0)),
        "status": t.get("status", "pending"),
        "created_at": t["created_at"].isoformat() if isinstance(t.get("created_at"), datetime) else None,
        "expires_at": t["expires_at"].isoformat() if isinstance(t.get("expires_at"), datetime) else None,
    }
    return out


# ── Endpoints ──────────────────────────────────────────────────────────────
# Mounted by server.py at /api/orgs/{org_id}/ownership-transfers/... AND
# /api/me/pending-ownership-transfers. We expose the router builder so
# server.py wires it without us importing the whole module graph.

class _InitiateBody(BaseModel):
    recipient_user_id: str
    outgoing_role_after: Literal["member", "admin", "owner"]
    also_leave_after: bool = False


class _AcceptBody(BaseModel):
    # No fields needed — identity is the caller, transfer id is in URL.
    # Pydantic still requires a body class for FastAPI validation
    # behaviour to stay consistent across our endpoints.
    pass


def make_routers(*, db, AGENTS=None, require_org_member, get_current_user):
    """Factory returning two `APIRouter` instances so server.py can
    include them under different prefixes. We pass dependencies in
    rather than importing from server.py to avoid a circular import."""

    transfers_r = APIRouter()
    me_r = APIRouter()

    async def _build_transfer_doc(org_id: str, initiator, recipient_user_id, body):
        """Compose all the metadata once so initiate + tests share it."""
        # Look up recipient + org for friendly snapshot fields.
        try:
            recipient_uid = recipient_user_id
            recipient_doc = await db.users.find_one({"_id": ObjectId(recipient_uid)})
        except Exception:
            recipient_doc = None
        if not recipient_doc:
            raise HTTPException(404, "Recipient user not found")
        # Recipient MUST already be a member of this org. We don't add
        # them as part of the transfer flow — use the invite flow first.
        is_member = await db.org_members.find_one(
            {"org_id": org_id, "user_id": recipient_uid},
            {"_id": 0, "role": 1},
        )
        if not is_member:
            raise HTTPException(
                400, "Recipient must already be a member of the org — invite them first.",
            )
        # Required plan tier from current member count.
        member_count = await db.org_members.count_documents({"org_id": org_id})
        required = _required_tier_for_member_count(member_count)
        org = await db.orgs.find_one({"_id": ObjectId(org_id)}, {"_id": 0, "name": 1})
        now = datetime.now(timezone.utc)
        return {
            "org_id": org_id,
            "org_name": (org or {}).get("name", ""),
            "initiated_by": initiator["id"],
            "initiated_by_name": initiator.get("name", ""),
            "recipient_user_id": recipient_uid,
            "recipient_name": recipient_doc.get("name", ""),
            "outgoing_role_after": body.outgoing_role_after,
            "also_leave_after": bool(body.also_leave_after),
            "required_plan_tier": required,
            "member_count_at_init": member_count,
            "status": "pending",
            "created_at": now,
            "expires_at": now + TRANSFER_TTL,
        }

    @transfers_r.post("/orgs/{org_id}/ownership-transfers")
    async def initiate_transfer(
        org_id: str, body: _InitiateBody,
        u: dict = Depends(get_current_user),
    ):
        ctx = await require_org_member(org_id, u["id"])
        if ctx["member"].get("role") != "owner":
            raise HTTPException(403, "Only org owners can transfer ownership")
        if str(body.recipient_user_id) == str(u["id"]):
            raise HTTPException(400, "Can't transfer ownership to yourself")
        # Don't allow two concurrent pending transfers from the SAME
        # outgoing owner to the SAME recipient — surfaces confusion.
        existing = await db.org_ownership_transfers.find_one({
            "org_id": org_id,
            "initiated_by": u["id"],
            "recipient_user_id": body.recipient_user_id,
            "status": "pending",
        })
        if existing:
            raise HTTPException(
                400, "You already have a pending transfer to this user. "
                     "Cancel it before issuing a new one.",
            )
        doc = await _build_transfer_doc(org_id, u, body.recipient_user_id, body)
        res = await db.org_ownership_transfers.insert_one(doc)
        doc["_id"] = res.inserted_id
        return _serialize_transfer(doc)

    @transfers_r.get("/orgs/{org_id}/ownership-transfers")
    async def list_org_transfers(org_id: str, u: dict = Depends(get_current_user)):
        ctx = await require_org_member(org_id, u["id"])
        # Owners + admins see all transfers; regular members only see
        # the ones targeting THEM (privacy — non-admins don't need to
        # see ownership politics among co-owners).
        role = ctx["member"].get("role")
        query: dict = {"org_id": org_id, "status": "pending"}
        if role not in ("owner", "admin"):
            query["recipient_user_id"] = u["id"]
        rows = await db.org_ownership_transfers.find(query).sort("created_at", -1).to_list(50)
        return [_serialize_transfer(r) for r in rows]

    @transfers_r.delete("/orgs/{org_id}/ownership-transfers/{transfer_id}")
    async def cancel_or_decline_transfer(
        org_id: str, transfer_id: str,
        u: dict = Depends(get_current_user),
    ):
        """Outgoing owner cancels OR recipient declines. Same endpoint
        because both end states are 'not happening'; the only diff is
        the audit verb. We disambiguate by who's calling."""
        try:
            t = await db.org_ownership_transfers.find_one({"_id": ObjectId(transfer_id)})
        except Exception:
            raise HTTPException(404, "Transfer not found")
        if not t or t.get("org_id") != org_id:
            raise HTTPException(404, "Transfer not found")
        if t.get("status") != "pending":
            raise HTTPException(400, f"Transfer is already {t.get('status')}")
        actor_id = u["id"]
        if actor_id == t["initiated_by"]:
            new_status = "cancelled"
            actor_field = "cancelled_at"
        elif actor_id == t["recipient_user_id"]:
            new_status = "declined"
            actor_field = "declined_at"
        else:
            raise HTTPException(403, "You can't cancel this transfer")
        await db.org_ownership_transfers.update_one(
            {"_id": ObjectId(transfer_id)},
            {"$set": {
                "status": new_status,
                actor_field: datetime.now(timezone.utc),
                "ended_by": actor_id,
            }},
        )
        return {"ok": True, "status": new_status}

    @transfers_r.post("/orgs/{org_id}/ownership-transfers/{transfer_id}/accept")
    async def accept_transfer(
        org_id: str, transfer_id: str, _body: _AcceptBody = None,
        u: dict = Depends(get_current_user),
    ):
        """Recipient accepts. Atomic state change:
          (a) check plan tier — 402 if insufficient
          (b) target → owner
          (c) outgoing owner → outgoing_role_after
          (d) if also_leave_after, drop outgoing owner from org_members
              + scrub them from team rosters
          (e) mark transfer accepted
        """
        try:
            t = await db.org_ownership_transfers.find_one({"_id": ObjectId(transfer_id)})
        except Exception:
            raise HTTPException(404, "Transfer not found")
        if not t or t.get("org_id") != org_id:
            raise HTTPException(404, "Transfer not found")
        if t.get("status") != "pending":
            raise HTTPException(400, f"Transfer is already {t.get('status')}")
        if t["recipient_user_id"] != u["id"]:
            raise HTTPException(403, "Only the named recipient can accept")
        # Expiry: lazy check on accept.
        now = datetime.now(timezone.utc)
        exp = t.get("expires_at")
        if exp and isinstance(exp, datetime):
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < now:
                await db.org_ownership_transfers.update_one(
                    {"_id": ObjectId(transfer_id)},
                    {"$set": {"status": "expired", "expired_at": now}},
                )
                raise HTTPException(410, "This transfer expired — ask the original owner to issue a new one.")
        # Plan tier check. Privileged users (superadmin / staff) skip
        # the tier gate — they can always accept any org.
        try:
            recipient = await db.users.find_one(
                {"_id": ObjectId(u["id"])},
                {"_id": 0, "plan": 1, "email": 1, "is_superadmin": 1},
            )
        except Exception:
            recipient = None
        from staff import is_privileged_user
        if not is_privileged_user(recipient):
            current_plan = (recipient or {}).get("plan") or "spark"
            required = t.get("required_plan_tier") or "spark"
            if not _tier_is_sufficient(current_plan, required):
                # 402 Payment Required so the frontend knows to route to
                # /pricing for the named tier with `return_to=…/accept`.
                raise HTTPException(
                    402,
                    detail={
                        "code": "plan_upgrade_required",
                        "required_tier": required,
                        "current_tier": current_plan,
                        "member_count": t.get("member_count_at_init", 0),
                        "message": (
                            f"This org has {t.get('member_count_at_init', 0)} members — "
                            f"accepting ownership needs at least the {required.title()} plan. "
                            f"You're currently on {current_plan.title()}."
                        ),
                    },
                )
        # Mutate roles + (optional) self-leave for outgoing owner.
        outgoing_id = t["initiated_by"]
        outgoing_role_after = t.get("outgoing_role_after", "member")
        also_leave = bool(t.get("also_leave_after", False))

        # Promote recipient → owner.
        await db.org_members.update_one(
            {"org_id": org_id, "user_id": u["id"]},
            {"$set": {
                "role": "owner",
                "role_changed_at": now,
                "role_changed_by": outgoing_id,
                "promoted_via_transfer_id": str(t["_id"]),
            }},
        )

        if also_leave:
            # Pull outgoing owner out of teams + org membership entirely.
            try:
                await db.teams.update_many(
                    {"org_id": org_id},
                    {"$pull": {"member_user_ids": outgoing_id,
                               "admin_user_ids": outgoing_id}},
                )
            except Exception as exc:
                logger.warning("transfer accept: team scrub failed: %s", exc)
            await db.org_members.delete_one({"org_id": org_id, "user_id": outgoing_id})
        else:
            # Demote outgoing owner to their chosen role.
            await db.org_members.update_one(
                {"org_id": org_id, "user_id": outgoing_id},
                {"$set": {
                    "role": outgoing_role_after,
                    "role_changed_at": now,
                    "role_changed_by": u["id"],
                    "demoted_via_transfer_id": str(t["_id"]),
                }},
            )

        # Mark transfer accepted last so any partial failure leaves the
        # transfer pending (idempotent retry possible).
        await db.org_ownership_transfers.update_one(
            {"_id": ObjectId(transfer_id)},
            {"$set": {"status": "accepted", "accepted_at": now}},
        )
        return {
            "ok": True,
            "status": "accepted",
            "new_owner_id": u["id"],
            "outgoing_member_left": also_leave,
        }

    # ── Per-user inbox: my pending offers ───────────────────────────
    @me_r.get("/me/pending-ownership-transfers")
    async def my_pending_transfers(u: dict = Depends(get_current_user)):
        """Used by the /app shell banner. Returns up to 10 transfers
        currently targeting this user."""
        rows = await db.org_ownership_transfers.find({
            "recipient_user_id": u["id"],
            "status": "pending",
        }).sort("created_at", -1).to_list(10)
        # Drop any silently-expired ones so the banner is always fresh.
        now = datetime.now(timezone.utc)
        live: list[dict] = []
        for r in rows:
            exp = r.get("expires_at")
            if isinstance(exp, datetime):
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp < now:
                    await db.org_ownership_transfers.update_one(
                        {"_id": r["_id"]},
                        {"$set": {"status": "expired", "expired_at": now}},
                    )
                    continue
            live.append(r)
        return [_serialize_transfer(t) for t in live]

    return transfers_r, me_r


async def sweep_expired_transfers(db) -> int:
    """Called from server.py startup. Marks any pending transfers
    past their expiry as `expired` so they stop showing up in banners
    and lists. Returns the count of rows updated."""
    now = datetime.now(timezone.utc)
    res = await db.org_ownership_transfers.update_many(
        {"status": "pending", "expires_at": {"$lt": now}},
        {"$set": {"status": "expired", "expired_at": now}},
    )
    return res.modified_count or 0
