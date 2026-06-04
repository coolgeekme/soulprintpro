"""Trial-tier per-resource usage caps.

This is the **conversion-pressure layer** on top of the time-bounded
access codes. When a user is on the implicit "trial" plan
(`access_expires_at` set, no active Stripe subscription), each call to
a cost-incurring endpoint goes through `consume_or_raise` which:

  1. Resolves whether the user is on trial, paid, perpetual, or expired
  2. Looks up their cap for the resource being consumed
  3. Increments their counter
  4. Raises HTTP 402 with an actionable upgrade message if over cap

Paid subscribers + grandfathered/staff/superadmin bypass all caps —
their cost ceiling is the Stripe-billed monthly budget (already
enforced in `billing.py`). Trial users get this stricter, granular
budget so they can taste every feature without burning unbounded
spend.

Why not lean on Stripe billing budgets for everyone? Because trial
users don't have a payment method on file — we can't fall back to
"charge them more". The only safety valve is a hard cap.

Window semantics: the trial counter resets each time the user
redeems a fresh access code (i.e. on each `access_extended_at`
bump). So a user who redeems a second 30-day code gets a fresh
allowance, which is exactly the "extend my trial" UX they expect.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import HTTPException

logger = logging.getLogger(__name__)


# ── Trial caps (per access window) ───────────────────────────────────
#
# Tuned for the Jun 2026 trial design (see PRD § "Trial period"):
# show the breadth of the platform but hard-cap the cost centers.
# Edit these numbers HERE — the Billing UI + cap-enforcement layer
# both read from this dict so they can never drift.

TRIAL_CAPS: Dict[str, Dict[str, Any]] = {
    "messages": {
        "limit": 200,
        "label": "Agent messages",
        "blurb": "Conversations with any of the 169 agents",
    },
    "images": {
        "limit": 20,
        "label": "Image generations",
        "blurb": "Cover images, social art, illustrations",
    },
    "missions": {
        "limit": 2,
        "label": "Mission spawns",
        "blurb": "Autonomous Crucible / Playwright missions",
    },
    "publishes": {
        "limit": 10,
        "label": "Social publishes",
        "blurb": "Posts sent to Twitter, LinkedIn, IG, FB, TikTok, YouTube",
    },
    "voice_minutes": {
        "limit": 10,
        "label": "Voice transcription",
        "blurb": "Whisper-transcribed voice notes (minutes)",
    },
    "audits": {
        "limit": 1,
        "label": "Signal Audits",
        "blurb": "Full audit on one website",
    },
}


# ── Plan resolution ──────────────────────────────────────────────────


def resolve_user_plan(user: Dict[str, Any]) -> Dict[str, Any]:
    """Decide which plan tier governs this user's caps.

    Returns ``{tier, reason}`` where ``tier ∈ {paid, trial, expired,
    perpetual, grace, none}``:
      • "paid"      — active Stripe subscription (spark/temper/refine)
      • "trial"     — has `access_expires_at` in the future, no sub
      • "grace"     — within 7-day soft-block grace period
      • "expired"   — past grace
      • "perpetual" — grandfathered / no expiry / superadmin / staff
      • "none"      — never granted access (gate denied)

    `reason` is a stable string the frontend can branch on for copy.
    Superadmin / staff overrides win over everything else so internal
    accounts never hit a cap.
    """
    if not user:
        return {"tier": "none", "reason": "anonymous"}
    if user.get("is_superadmin"):
        return {"tier": "perpetual", "reason": "superadmin"}
    if user.get("is_staff"):
        return {"tier": "perpetual", "reason": "staff"}
    # Paid subscription overrides everything — once the user pays,
    # the Stripe budget governs cost, not these trial caps.
    plan_status = (user.get("plan_status") or "").lower()
    if plan_status in ("active", "trialing", "past_due"):
        return {"tier": "paid", "reason": f"plan_{plan_status}"}
    # Check the time-bounded access window from access_codes.py
    access_granted = user.get("access_granted_at")
    access_expires = user.get("access_expires_at")
    if not access_granted:
        return {"tier": "none", "reason": "no_access"}
    # No expiry set = grandfathered / perpetual access (existing users
    # before Jun 2026, or codes minted with grants_duration_days = null)
    if not isinstance(access_expires, datetime):
        return {"tier": "perpetual", "reason": "grandfathered"}
    if access_expires.tzinfo is None:
        access_expires = access_expires.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if access_expires > now:
        return {"tier": "trial", "reason": "active_window"}
    # Past expiry. Check grace.
    from datetime import timedelta
    from access_codes import ACCESS_GRACE_DAYS
    grace_deadline = access_expires + timedelta(days=ACCESS_GRACE_DAYS)
    if now <= grace_deadline:
        return {"tier": "grace", "reason": "grace_period"}
    return {"tier": "expired", "reason": "expired"}


# ── Counter persistence ──────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _get_or_init_counters(db, user_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch the user's trial-counter row, or create it if missing.

    The counter row is bound to the user's CURRENT access window —
    keyed by `(user_id, access_extended_at)` so redeeming a fresh
    code automatically starts a new counter without us having to
    reset the old one explicitly. Old counters stay in the DB for
    auditability.

    Returns the row dict (with `_id` excluded from the response).
    """
    window_key = user.get("access_extended_at") or user.get("access_granted_at")
    if not isinstance(window_key, datetime):
        # Perpetual user — no window. Return an inert counter that
        # `consume_or_raise` will skip because plan resolution lands
        # on "perpetual".
        return {"user_id": user_id, "window_start": None, "counts": {}}
    if window_key.tzinfo is None:
        window_key = window_key.replace(tzinfo=timezone.utc)
    row = await db.trial_counters.find_one(
        {"user_id": user_id, "window_start": window_key},
        {"_id": 0},
    )
    if row:
        return row
    row = {
        "user_id": user_id,
        "window_start": window_key,
        "counts": {k: 0 for k in TRIAL_CAPS.keys()},
        "first_created_at": _now(),
        "last_consumed_at": None,
    }
    await db.trial_counters.insert_one(row)
    # Mongo mutated the dict; drop _id before returning.
    row.pop("_id", None)
    return row


# ── The two helpers the rest of the codebase calls ───────────────────


async def consume_or_raise(db, user: Dict[str, Any], resource: str, amount: int = 1) -> None:
    """Increment the user's counter for `resource` (e.g. "images") by
    `amount`. Raises HTTP 402 if the user is on trial AND the new
    total exceeds the cap.

    Paid / perpetual / superadmin / staff bypass entirely — they
    never hit this rail. Expired / grace users get the same 402 the
    `access_codes.assert_full_access` helper already issues.

    Idempotency: caller is responsible for not double-charging. We
    only debit when called.
    """
    if resource not in TRIAL_CAPS:
        # Programmer error — don't silently skip, but don't 500 either.
        logger.warning("consume_or_raise: unknown resource %r", resource)
        return
    if not user or not user.get("id"):
        raise HTTPException(401, "Not authenticated")
    # Re-fetch a lean copy so we have access_expires_at / plan_status
    # fresh on every consume call (cached `user` dicts from /me may
    # be stale by minutes).
    try:
        fresh = await db.users.find_one(
            {"_id": ObjectId(user["id"])},
            {
                "is_superadmin": 1, "is_staff": 1,
                "plan_status": 1, "plan_id": 1,
                "access_granted_at": 1, "access_expires_at": 1,
                "access_extended_at": 1,
            },
        ) or {}
    except Exception:
        fresh = {}
    fresh["id"] = user["id"]
    decision = resolve_user_plan(fresh)
    tier = decision["tier"]
    if tier == "paid" or tier == "perpetual":
        return  # No cap — Stripe budget governs cost.
    if tier in ("expired", "none"):
        raise HTTPException(
            402,
            "Your access has expired. Redeem an access code "
            "(Billing → Extend access) to continue.",
        )
    if tier == "grace":
        raise HTTPException(
            402,
            "You're in a 7-day read-only grace period. Redeem an "
            "access code (Billing → Extend access) to keep "
            "drafting / publishing.",
        )
    # tier == "trial" → enforce cap
    cap = TRIAL_CAPS[resource]["limit"]
    counters = await _get_or_init_counters(db, user["id"], fresh)
    used = (counters.get("counts") or {}).get(resource) or 0
    if used + amount > cap:
        label = TRIAL_CAPS[resource]["label"].lower()
        raise HTTPException(
            402,
            f"You've used all {cap} {label} included in your trial "
            f"window. Subscribe (Billing → Subscribe) to unlock "
            f"unlimited usage, or redeem an extension code to "
            f"reset the trial counter.",
        )
    # Persist the increment.
    window_key = fresh.get("access_extended_at") or fresh.get("access_granted_at")
    if isinstance(window_key, datetime) and window_key.tzinfo is None:
        window_key = window_key.replace(tzinfo=timezone.utc)
    await db.trial_counters.update_one(
        {"user_id": user["id"], "window_start": window_key},
        {
            "$inc": {f"counts.{resource}": amount},
            "$set": {"last_consumed_at": _now()},
        },
    )


async def get_trial_usage(db, user: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only view of the user's trial usage + caps. Powers the
    Billing-page meter cards and the TopBar pill's "12 imgs left"
    sub-line. Returns ``None`` for non-trial users (so the UI can
    hide the panel entirely)."""
    if not user or not user.get("id"):
        return {"tier": "none", "reason": "anonymous", "caps": {}}
    try:
        fresh = await db.users.find_one(
            {"_id": ObjectId(user["id"])}
        ) or {}
    except Exception:
        fresh = {}
    fresh["id"] = user["id"]
    decision = resolve_user_plan(fresh)
    out = {
        "tier": decision["tier"],
        "reason": decision["reason"],
        "access_expires_at": (
            fresh.get("access_expires_at").isoformat()
            if isinstance(fresh.get("access_expires_at"), datetime)
            else None
        ),
        "caps": {},
    }
    if decision["tier"] != "trial":
        # Non-trial users — return tier + zero caps payload so the
        # UI can short-circuit without a separate "is on trial" flag.
        return out
    counters = await _get_or_init_counters(db, user["id"], fresh)
    used = counters.get("counts") or {}
    for resource, meta in TRIAL_CAPS.items():
        cap = meta["limit"]
        consumed = used.get(resource) or 0
        out["caps"][resource] = {
            "label": meta["label"],
            "blurb": meta["blurb"],
            "limit": cap,
            "used": consumed,
            "remaining": max(0, cap - consumed),
            "percent": min(100, round((consumed / cap) * 100)) if cap else 0,
        }
    return out


# ─── Add-on credit accounting ────────────────────────────────────────────
# Each one-time pack (Message / Browser / Mission) tops up a per-user
# counter on the `users` row. We debit those counters BEFORE falling
# back to plan-quota logic — so a user who topped up a message pack
# isn't pointlessly rate-limited just because the trial quota would
# normally fire.
#
# Maps add-on credit field → which `consume_or_raise` resource it
# substitutes for. Used by `consume_credit_or_quota` to pick the right
# fallback path when the credit balance hits zero.
_CREDIT_TO_RESOURCE = {
    "credit_messages": "messages",
    "credit_missions": "missions",
    # `credit_browser_minutes` doesn't map to a trial-cap resource;
    # browser minutes aren't currently in TRIAL_CAPS. The helper
    # short-circuits to "free" (no fallback) for that one — see below.
}


async def _debit_credit(db, user_id: str, credit_field: str, amount: int) -> int:
    """Atomically debit ``credit_field`` by ``amount``, never going
    below zero. Returns the number of units actually debited (≤ amount).

    Uses a single conditional update so multiple concurrent requests
    can't over-spend a credit balance. If the balance was less than
    ``amount``, only the available portion is debited and the rest is
    the caller's responsibility (falls back to plan quota).
    """
    if amount <= 0:
        return 0
    try:
        user_oid = ObjectId(user_id)
    except Exception:
        return 0
    # Step 1 — read current balance.
    u = await db.users.find_one(
        {"_id": user_oid}, {"_id": 0, credit_field: 1},
    ) or {}
    current = max(0, int(u.get(credit_field) or 0))
    if current <= 0:
        return 0
    to_debit = min(current, amount)
    # Step 2 — atomic conditional decrement. If another request just
    # raced us we retry once with the fresh balance; after that we
    # bail (caller falls back to quota for the remainder).
    for _ in range(2):
        res = await db.users.update_one(
            {"_id": user_oid, credit_field: {"$gte": to_debit}},
            {"$inc": {credit_field: -to_debit}},
        )
        if res.modified_count:
            return to_debit
        # Re-read and try again with the smaller available balance.
        u = await db.users.find_one(
            {"_id": user_oid}, {"_id": 0, credit_field: 1},
        ) or {}
        current = max(0, int(u.get(credit_field) or 0))
        if current <= 0:
            return 0
        to_debit = min(current, amount)
    return 0


async def consume_credit_or_quota(
    db, user: Dict[str, Any], credit_field: str, amount: int = 1,
) -> None:
    """Try to debit add-on credit first; on remainder, fall back to the
    trial-quota path.

    Call this at the START of any cost-incurring user action that maps
    to a one-time pack. The function is idempotent w.r.t. partial
    debits — e.g. 8 left in credit_messages + amount=10 → debits 8 + asks
    quota for the remaining 2 + raises 402 if quota would also exceed.

    For `credit_browser_minutes` there's no trial-cap fallback (the
    user's primary access-control is the mission cap which is checked
    elsewhere). We debit whatever's available and silently let any
    overage through — same UX as today, just metered.
    """
    if not user or not user.get("id"):
        return
    fallback_resource = _CREDIT_TO_RESOURCE.get(credit_field)
    debited = await _debit_credit(db, user["id"], credit_field, amount)
    remaining = max(0, amount - debited)
    if remaining <= 0:
        return
    if fallback_resource:
        # Push the leftover through the existing quota path so trial
        # users still get blocked once they exhaust everything.
        await consume_or_raise(db, user, fallback_resource, amount=remaining)
    # credit_browser_minutes path → silently allow overage (no quota).


async def consume_credit_after_use(
    db, user: Dict[str, Any], credit_field: str, amount: int,
) -> int:
    """Settle USAGE after the fact (e.g. mission browser minutes).
    Returns the number of credits actually debited.

    Use this for resources where we don't know the exact cost upfront
    — start the mission cheaply, finish it, then call this with the
    real elapsed minutes. Never raises; if the balance can't cover
    the full amount we still drain it (the user got the value, no
    point clawing back partial work).
    """
    if not user or not user.get("id") or amount <= 0:
        return 0
    return await _debit_credit(db, user["id"], credit_field, amount)


async def get_addon_credit_balances(
    db, user_id: str,
) -> Dict[str, int]:
    """Read-only fetch of all four credit balances. Powers the billing
    page + admin views without hitting the user row twice."""
    try:
        user_oid = ObjectId(user_id)
    except Exception:
        return {"credit_messages": 0, "credit_missions": 0,
                "credit_browser_minutes": 0, "extra_orgs": 0,
                "extra_seats": 0}
    u = await db.users.find_one(
        {"_id": user_oid},
        {"_id": 0, "credit_messages": 1, "credit_missions": 1,
         "credit_browser_minutes": 1, "extra_orgs": 1, "extra_seats": 1},
    ) or {}
    return {
        "credit_messages": int(u.get("credit_messages") or 0),
        "credit_missions": int(u.get("credit_missions") or 0),
        "credit_browser_minutes": int(u.get("credit_browser_minutes") or 0),
        "extra_orgs": int(u.get("extra_orgs") or 0),
        "extra_seats": int(u.get("extra_seats") or 0),
    }


__all__ = [
    "TRIAL_CAPS",
    "resolve_user_plan",
    "consume_or_raise",
    "consume_credit_or_quota",
    "consume_credit_after_use",
    "get_trial_usage",
    "get_addon_credit_balances",
]
