"""Seat-policy helper for soft-tier enforcement on org invites.

Foundry sells Spark as "1 user · extra seats $30/mo", but the backend
historically did not enforce a seat cap. We don't want to hard-block
inviting (existing customers would scream) — we want a friendly
in-product warning that nudges admins toward upgrading or buying seat
add-ons.

Special case: **access-code owners**
====================================
Owners who joined via one of our pre-launch private access codes (or
a 24-hour trial code) get a free Temper-equivalent seat allowance
(5 seats) for the lifetime of the code. When the code expires the
allowance drops back to whatever the org's actual subscription is —
the warning banner re-appears immediately.

Schema notes:
  • Org doc may have `plan` ∈ {"spark","temper","refine"} (default spark).
  • User doc carries `access_code_id` + `access_code_used` + the
    code's expiry comes from `db.access_codes.expires_at`.
  • Trial path stamps `trial_expires_at` directly on the user.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId

logger = logging.getLogger(__name__)


SEATS_INCLUDED = {
    "spark": 1,
    "temper": 5,
    "refine": 10_000,  # "effectively unlimited" — fair-use beyond this
}


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if not isinstance(dt, datetime):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def _owner_access_code_expiry(db, owner_user_id: str) -> Optional[datetime]:
    """Return the expiry of the owner's still-valid access/trial code,
    or None if there is no active boost.

    Considers both buckets:
      1. Private/admin access codes (db.access_codes)
      2. Trial codes (24h timer stamped on user doc)
    """
    try:
        user_oid = ObjectId(owner_user_id)
    except Exception:
        return None
    user = await db.users.find_one(
        {"_id": user_oid},
        {"access_code_id": 1, "trial_expires_at": 1},
    )
    if not user:
        return None
    now = datetime.now(timezone.utc)

    # 1. Trial path — short-lived 24h timer.
    t_exp = _aware(user.get("trial_expires_at"))
    if t_exp and t_exp > now:
        return t_exp

    # 2. Private access code — may have a far-future expiry or no
    #    expiry at all (perpetual boost while the code remains active).
    code_id = user.get("access_code_id")
    if code_id:
        try:
            row = await db.access_codes.find_one({"_id": ObjectId(code_id)})
        except Exception:
            row = None
        if row and row.get("is_active", True):
            exp = _aware(row.get("expires_at"))
            if exp is None:
                # Code has no expiry — boost lasts until admin
                # deactivates it. Surface a stable far-future date so
                # the UI can render "permanent" cleanly.
                return datetime(2999, 1, 1, tzinfo=timezone.utc)
            if exp > now:
                return exp
    return None


async def _find_owner_user_id(db, org_id: str) -> Optional[str]:
    """Return the user_id of any owner of this org. Most orgs have
    exactly one owner; if there are multiple we return whichever Mongo
    surfaces first — the boost applies if ANY owner is access-code
    backed, which is the most generous (and least likely to cause
    surprise) interpretation."""
    member = await db.org_members.find_one(
        {"org_id": org_id, "role": "owner"},
        {"user_id": 1},
    )
    return member["user_id"] if member else None


async def compute_seat_status(db, org_id: str) -> Dict[str, Any]:
    """Single source of truth for "how many seats does this org get,
    and how many has it used?". Used by both the invite endpoint
    (to attach a warning) and the dedicated /seat-status endpoint
    (to render the in-product banner).

    Returns:
      {
        "plan": str,                     # plan field on the org
        "effective_plan": str,           # plan + access-code boost
        "seats_included": int,
        "seats_used": int,               # members + pending invites
        "members": int,
        "pending_invites": int,
        "over_cap": bool,                # current count > allowance
        "next_invite_over_cap": bool,    # +1 would exceed
        "boost_source": str|None,        # "access_code" if boosted
        "boost_expires_at": str|None,    # ISO date or "permanent"-ish
      }
    """
    try:
        org_oid = ObjectId(org_id)
    except Exception:
        return {}
    org = await db.organizations.find_one({"_id": org_oid}, {"plan": 1})
    plan = (org or {}).get("plan") or "spark"
    base_seats = SEATS_INCLUDED.get(plan, 1)

    seats_included = base_seats
    boost_source: Optional[str] = None
    boost_expires_at: Optional[datetime] = None

    # Apply the access-code boost only when it would actually help —
    # an owner on Refine already has unlimited seats, no need.
    owner_uid = await _find_owner_user_id(db, org_id)
    if owner_uid and base_seats < SEATS_INCLUDED["temper"]:
        code_exp = await _owner_access_code_expiry(db, owner_uid)
        if code_exp is not None:
            seats_included = SEATS_INCLUDED["temper"]
            boost_source = "access_code"
            boost_expires_at = code_exp

    members = await db.org_members.count_documents({"org_id": org_id})
    pending = await db.invites.count_documents(
        {"org_id": org_id, "used_by": None},
    )
    seats_used = members + pending

    effective_plan = "temper" if boost_source else plan

    return {
        "plan": plan,
        "effective_plan": effective_plan,
        "seats_included": seats_included,
        "seats_used": seats_used,
        "members": members,
        "pending_invites": pending,
        "over_cap": seats_used > seats_included,
        "next_invite_over_cap": (seats_used + 1) > seats_included,
        "boost_source": boost_source,
        "boost_expires_at": (
            boost_expires_at.isoformat() if boost_expires_at else None
        ),
    }


def warning_for_status(status: Dict[str, Any]) -> Optional[str]:
    """Human-readable warning when an invite would push the org over
    its seat cap. None when the org is comfortably within its plan.

    Soft enforcement: the invite still goes through. This string is
    surfaced in the API response and shown as a toast / banner on the
    invite UI so the admin knows they're racking up overage.
    """
    if not status:
        return None
    if status.get("plan") == "refine":
        return None
    if not status.get("over_cap") and not status.get("next_invite_over_cap"):
        return None
    plan = status.get("effective_plan", "spark")
    seats_used = status.get("seats_used", 0)
    seats_included = status.get("seats_included", 1)
    # Access-code boost gives Temper-equivalent allowance. If they're
    # still within that allowance, suppress the nag. If they've blown
    # past 5 seats even with the boost, show a Temper-shaped warning
    # so they know the free ride doesn't cover unlimited seats.
    boosted = status.get("boost_source") == "access_code"
    if plan == "spark":
        return (
            f"Heads up — {seats_used} of {seats_included} included Spark "
            f"seat used. You have two options: (a) add seats à-la-carte "
            f"for $30/mo each on top of your $80/mo Spark plan, or "
            f"(b) upgrade to Temper for $300/mo (5 seats included) — "
            f"breaks even around your 4th teammate."
        )
    if plan == "temper":
        prefix = (
            "Your access-code boost gives you Temper-equivalent seats, "
            "but you've now used more than 5. "
        ) if boosted else ""
        return (
            f"{prefix}Heads up — {seats_used} of {seats_included} included "
            f"Temper seats used. Each extra seat is $40/mo on top of your "
            f"$300/mo Temper plan. Talk to us about Refine for "
            f"unlimited seats."
        )
    return None
