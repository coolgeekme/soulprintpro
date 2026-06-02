"""Pre-launch site access gate.

Two parallel sources of truth grant a signed-in user passage past the
gate:

 1. Their user row carries ``access_granted_at`` after they redeemed
    a private/admin-issued access code OR a 24-hour trial code at any
    point in the past. (Trial redemption stamps it implicitly via
    ``trial_started_at`` — see the `_user_has_access` helper.)
 2. Their browser carries a long-lived ``sp_access_ok`` JWT cookie
    that was minted at redeem-time. The cookie is honoured even if
    the current account is fresh — this is per the user's product
    decision: "bound to account AND device cookie", i.e. once a
    device has been greenlit, anyone signing in from that device
    walks straight in.

Superadmins always pass.

Codes:

* **Trial codes** (existing collection ``trial_codes``) — single-use,
  email-bound, started a 24h timer. Already wired through
  ``trial.py``. The redeem endpoint here ALSO accepts these codes for
  signed-in users who never registered through the trial funnel and
  just need to unlock their account post-hoc.

* **Private access codes** (new collection ``access_codes``) — admin
  generated. Either single-use (one redemption) or multi-use (a
  shared cohort code), with optional expiry.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────

# Long-lived browser cookie set when the device first redeems any code.
# Same JWT secret as the auth tokens (HS256). 1 year max-age.
ACCESS_COOKIE_NAME = "sp_access_ok"
ACCESS_COOKIE_MAX_AGE = 365 * 24 * 60 * 60

# Codes use a non-ambiguous alphabet (no 0/O/1/l/I) and are 10 chars
# long. 28 alphabet × 10 ≈ 3e14 combos — collision-safe at any sane
# admin issuance volume.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_code(s: str) -> str:
    return (s or "").upper().strip().replace(" ", "").replace("-", "")


def _generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def _jwt_secret() -> str:
    # Same secret as auth tokens — server.py reads it at boot. Keep this
    # lazy so import order doesn't matter.
    return os.environ.get("JWT_SECRET") or os.environ.get(
        "SECRET_KEY", "soulprint-dev-secret-change-me",
    )


def mint_device_cookie(code_id: str) -> str:
    """JWT that proves THIS browser successfully redeemed an access code.
    Used purely to short-circuit re-prompting on a known device, never
    to grant any other privilege.
    """
    return jwt.encode(
        {
            "kind": "access_device",
            "code_id": code_id,
            "exp": _now() + timedelta(seconds=ACCESS_COOKIE_MAX_AGE),
        },
        _jwt_secret(),
        algorithm="HS256",
    )


def _verify_device_cookie(token: str) -> Optional[str]:
    if not token:
        return None
    try:
        claims = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except Exception:
        return None
    if claims.get("kind") != "access_device":
        return None
    return claims.get("code_id") or ""


def set_access_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        ACCESS_COOKIE_NAME,
        token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=ACCESS_COOKIE_MAX_AGE,
        path="/",
    )


# ── Access decision ───────────────────────────────────────────────────────


def _user_has_access(user: dict) -> tuple[bool, str]:
    """Pure-function gate decision for a signed-in user. Returns
    (allowed, reason). Reasons are stable strings the frontend can
    branch on for copy:
        "superadmin" / "private_code" / "trial" / "grace_period" /
        "expired" / "none"
    """
    if not user:
        return False, "none"
    if user.get("is_superadmin"):
        return True, "superadmin"
    # Active or even past-but-redeemed trial counts — once you've
    # touched the trial flow, we don't slam the door behind you.
    if user.get("trial_started_at"):
        return True, "trial"
    if not user.get("access_granted_at"):
        return False, "none"
    # Time-bounded access (Jun 2026): if the user redeemed a code that
    # carried a duration, their access window expires at the stamped
    # datetime. Grandfathered + perpetual users have no
    # `access_expires_at` and stay green forever.
    expires_at = user.get("access_expires_at")
    if not isinstance(expires_at, datetime):
        return True, "private_code"
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now = _now()
    if expires_at > now:
        return True, "private_code"
    grace_deadline = expires_at + timedelta(days=ACCESS_GRACE_DAYS)
    if now <= grace_deadline:
        return True, "grace_period"
    return False, "expired"


def access_state(user: dict) -> dict:
    """Rich access-state object consumed by the TopBar pill, the
    Billing page section, and the read-only banner. Frontend reads
    `expires_at` / `days_remaining` / `mode` to decide what to show.

    `mode` ∈ {"full", "readonly", "blocked"}:
      • "full"     — user can perform every action
      • "readonly" — within the 7-day grace period after expiry;
                     reads pass, writes (draft / publish / mission)
                     are refused with a 402 "extend access"
      • "blocked"  — past grace; every request bounces to /access-expired
    """
    if not user:
        return {"mode": "blocked", "reason": "none",
                "expires_at": None, "days_remaining": None,
                "in_grace": False, "grace_days_remaining": None,
                "is_perpetual": False}
    if user.get("is_superadmin"):
        return {"mode": "full", "reason": "superadmin",
                "expires_at": None, "days_remaining": None,
                "in_grace": False, "grace_days_remaining": None,
                "is_perpetual": True}
    allowed, reason = _user_has_access(user)
    expires_at = user.get("access_expires_at")
    if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now = _now()
    iso_expiry = expires_at.isoformat() if isinstance(expires_at, datetime) else None
    if not allowed:
        return {"mode": "blocked", "reason": reason,
                "expires_at": iso_expiry, "days_remaining": 0,
                "in_grace": False, "grace_days_remaining": 0,
                "is_perpetual": False}
    if not isinstance(expires_at, datetime):
        # Perpetual access (grandfathered, trial-only, or pre-duration code)
        return {"mode": "full", "reason": reason,
                "expires_at": None, "days_remaining": None,
                "in_grace": False, "grace_days_remaining": None,
                "is_perpetual": True}
    delta_secs = (expires_at - now).total_seconds()
    days_remaining = max(0, int(delta_secs // 86400))
    if reason == "grace_period":
        grace_deadline = expires_at + timedelta(days=ACCESS_GRACE_DAYS)
        grace_secs = (grace_deadline - now).total_seconds()
        return {"mode": "readonly", "reason": "grace_period",
                "expires_at": iso_expiry, "days_remaining": 0,
                "in_grace": True,
                "grace_days_remaining": max(0, int(grace_secs // 86400)),
                "is_perpetual": False}
    return {"mode": "full", "reason": reason,
            "expires_at": iso_expiry, "days_remaining": days_remaining,
            "in_grace": False, "grace_days_remaining": None,
            "is_perpetual": False}


async def assert_full_access(db, user: dict) -> None:
    """Raise 402 if the user is in the soft-block (grace) window.
    Sprinkled into the cost-incurring write endpoints (draft / image
    generation / publish / mission). Read endpoints never call this
    so grace users can still BROWSE everything they built — they
    just can't burn new credits.
    """
    # Re-hydrate to ensure we have the latest `access_expires_at`
    # (the in-flight `user` dict may be lean / stale).
    if user and user.get("id"):
        try:
            full = await db.users.find_one({"_id": ObjectId(user["id"])})
            if full:
                user = full
        except Exception:
            pass
    st = access_state(user)
    if st["mode"] == "full":
        return
    if st["mode"] == "readonly":
        raise HTTPException(
            402,
            "Your access window has expired and you're in a 7-day "
            "read-only period. Redeem a fresh access code (Billing → "
            "Extend access) to keep drafting and publishing.",
        )
    raise HTTPException(
        402,
        "Your access has expired. Redeem an access code to continue.",
    )


async def device_cookie_grants_access(db, request: Request) -> bool:
    """Honours the device cookie if the underlying code is still
    active (admins may revoke a leaked code, in which case every
    device that redeemed it is re-prompted on next page load)."""
    code_id = _verify_device_cookie(request.cookies.get(ACCESS_COOKIE_NAME, ""))
    if not code_id:
        return False
    try:
        row = await db.access_codes.find_one({"_id": ObjectId(code_id)})
    except Exception:
        return False
    if not row:
        return False
    if not row.get("is_active", True):
        return False
    expires = row.get("expires_at")
    if isinstance(expires, datetime):
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < _now():
            return False
    return True


# ── Shared redemption helpers ─────────────────────────────────────────────


async def validate_code_only(db, raw_code: str) -> tuple[str, dict] | None:
    """Lightweight check: does this code currently grant access?
    Returns ``(kind, row)`` where ``kind`` ∈ {"private", "trial"} on
    match, ``None`` otherwise. Doesn't redeem — just confirms validity
    so callers (like /auth/register) can short-circuit BEFORE mutating
    the user row.
    """
    code = _norm_code(raw_code)
    if not code:
        return None
    now = _now()
    row = await db.access_codes.find_one({"code": code})
    if row:
        if not row.get("is_active", True):
            return None
        expires = row.get("expires_at")
        if isinstance(expires, datetime):
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < now:
                return None
        if row.get("usage_type") == "single":
            if (row.get("used_count") or 0) >= 1:
                return None
        else:  # multi
            cap = row.get("max_uses")
            if cap is not None and (row.get("used_count") or 0) >= cap:
                return None
        return "private", row
    trial_row = await db.trial_codes.find_one({"code": code})
    if trial_row:
        if trial_row.get("used_at"):
            return None
        t_expires = trial_row.get("expires_at")
        if isinstance(t_expires, datetime):
            if t_expires.tzinfo is None:
                t_expires = t_expires.replace(tzinfo=timezone.utc)
            if t_expires < now:
                return None
        return "trial", trial_row
    return None


async def redeem_code_for_user(db, raw_code: str, user_id: str) -> tuple[str, str] | None:
    """Burn a code on behalf of ``user_id``. Used by /auth/register so
    a code can be redeemed AT signup time in a single round-trip
    instead of forcing the user through a post-auth gate.

    Stamps ``access_granted_at`` on the user row and returns
    ``(reason, code_id_for_cookie)`` so the caller can mint the
    device cookie. Returns ``None`` if the code is invalid (the
    caller is expected to have already validated, but this is a
    second line of defence against TOCTOU).
    """
    code = _norm_code(raw_code)
    if not code:
        return None
    now = _now()
    try:
        user_obj_id = ObjectId(user_id)
    except Exception:
        return None
    row = await db.access_codes.find_one({"code": code})
    if row:
        if not row.get("is_active", True):
            return None
        expires = row.get("expires_at")
        if isinstance(expires, datetime):
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < now:
                return None
        already = str(user_obj_id) in (row.get("redeemed_by_user_ids") or [])
        if not already:
            if row.get("usage_type") == "single":
                if (row.get("used_count") or 0) >= 1:
                    return None
            else:
                cap = row.get("max_uses")
                if cap is not None and (row.get("used_count") or 0) >= cap:
                    return None
            await db.access_codes.update_one(
                {"_id": row["_id"]},
                {
                    "$inc": {"used_count": 1},
                    "$addToSet": {"redeemed_by_user_ids": str(user_obj_id)},
                    "$set": {"last_used_at": now},
                },
            )
        # Stacking math (Jun 2026): if the code carries a duration,
        # extend `access_expires_at` by that many days. We stack from
        # `max(current expiry, now)` so a user who still has 10 days
        # left + redeems a 30-day code ends up with 40 days remaining
        # (not 30). Perpetual users (no `grants_duration_days` on
        # this code) keep their unlimited window.
        user_updates: dict = {
            "access_granted_at": now,
            "access_code_used": code,
            "access_code_id": str(row["_id"]),
        }
        duration_days = row.get("grants_duration_days")
        if isinstance(duration_days, int) and duration_days > 0:
            existing = await db.users.find_one(
                {"_id": user_obj_id}, {"access_expires_at": 1}
            ) or {}
            current_expiry = existing.get("access_expires_at")
            if isinstance(current_expiry, datetime):
                if current_expiry.tzinfo is None:
                    current_expiry = current_expiry.replace(tzinfo=timezone.utc)
                base = max(current_expiry, now)
            else:
                base = now
            user_updates["access_expires_at"] = base + timedelta(days=duration_days)
            user_updates["access_extended_at"] = now
        await db.users.update_one(
            {"_id": user_obj_id},
            {"$set": user_updates},
        )
        return "private_code", str(row["_id"])
    trial_row = await db.trial_codes.find_one({"code": code})
    if trial_row:
        if trial_row.get("used_at"):
            return None
        t_expires = trial_row.get("expires_at")
        if isinstance(t_expires, datetime):
            if t_expires.tzinfo is None:
                t_expires = t_expires.replace(tzinfo=timezone.utc)
            if t_expires < now:
                return None
        trial_started_at = now
        trial_expires_at = now + timedelta(hours=24)
        await db.users.update_one(
            {"_id": user_obj_id},
            {"$set": {
                "access_granted_at": now,
                "access_code_used": code,
                "trial_started_at": trial_started_at,
                "trial_expires_at": trial_expires_at,
                "trial_source": trial_row.get("source") or "auth_register",
            }},
        )
        await db.trial_codes.update_one(
            {"_id": trial_row["_id"]},
            {"$set": {"used_at": now, "used_by_user_id": str(user_obj_id)}},
        )
        return "trial", str(trial_row["_id"])
    return None


async def grandfather_existing_users(db) -> int:
    """One-shot migration: stamp ``access_granted_at`` on every user
    row that doesn't already have it. Runs on backend startup; idempotent
    (the ``$exists: false`` filter means re-running does nothing).

    Rationale: when the access gate launches, every user who registered
    BEFORE it would otherwise be locked out until an admin minted them
    a code. That's product-hostile — they're paying customers we
    trust. This grandfathers them in one query.
    """
    res = await db.users.update_many(
        {"access_granted_at": {"$exists": False}},
        {"$set": {
            "access_granted_at": _now(),
            "access_code_used": "grandfathered",
        }},
    )
    return res.modified_count





class RedeemBody(BaseModel):
    code: str = Field(..., min_length=4, max_length=40)


class CreateCodeBody(BaseModel):
    # Optional — if omitted we mint a fresh random code. Admins can
    # type a memorable one for VIP invitees.
    code: Optional[str] = Field(None, max_length=40)
    usage_type: str = Field("single", pattern="^(single|multi)$")
    # Only meaningful for multi-use codes. None = unlimited.
    max_uses: Optional[int] = Field(None, ge=1, le=100000)
    # ISO datetime string. None = the CODE itself never goes stale
    # (separate from `grants_duration_days` below which controls how
    # long the *user* gets once they redeem).
    expires_at: Optional[str] = None
    # How many days of platform access this code GRANTS to each
    # redeemer. None = perpetual access (current behaviour — every
    # code shipped before Jun 2026 grants forever). Integer = stacks
    # onto the user's `access_expires_at` window per redemption
    # (so a 30-day code redeemed when the user has 10 days left =
    # 40 days remaining — the "extend my trial" use case).
    grants_duration_days: Optional[int] = Field(None, ge=1, le=3650)
    note: Optional[str] = Field(None, max_length=240)


# 7-day soft-block window after `access_expires_at` lapses. During
# the grace period the frontend renders a read-only banner and the
# backend lets read endpoints through but blocks cost-incurring
# writes (draft / image-gen / publish / mission). After the grace
# window the user is hard-cut and must redeem a fresh code.
ACCESS_GRACE_DAYS = 7


# ── Router factory ────────────────────────────────────────────────────────


def make_access_codes_router(*, db, get_current_user, get_user_or_none) -> APIRouter:
    """Mounts BOTH the public `/api/access-codes/*` and admin
    `/api/admin/access-codes/*` surfaces under one APIRouter so server.py
    only needs a single include line."""
    r = APIRouter()

    async def require_superadmin(u: dict = Depends(get_current_user)) -> dict:
        if not u.get("is_superadmin"):
            raise HTTPException(403, "Superadmin only")
        return u

    # ── Public: status check ──────────────────────────────────────────
    @r.get("/api/access-codes/status")
    async def status(request: Request):
        """Tells the SPA whether the current user (if any) is allowed
        through the pre-launch gate. Public-safe: returns
        ``{signed_in: false}`` with no auth — the frontend uses that
        to decide whether to bounce to /auth."""
        u = await get_user_or_none(request)
        if not u:
            # Even anonymous: a device cookie alone isn't enough — they
            # still need to sign in. But we can hint to the form that
            # "this device is whitelisted" so we don't re-prompt for a
            # code post-login on this device.
            device_ok = await device_cookie_grants_access(db, request)
            return {
                "signed_in": False,
                "has_access": False,
                "device_grant": device_ok,
                "reason": "anonymous",
            }
        # Hydrate the full row — get_current_user only returns lean
        # dicts in some code paths.
        try:
            full = await db.users.find_one({"_id": ObjectId(u["id"])})
        except Exception:
            full = None
        full = full or u
        allowed, reason = _user_has_access(full)
        if not allowed:
            # Device cookie can rescue them — per product decision the
            # gate is satisfied if EITHER account or device says yes.
            if await device_cookie_grants_access(db, request):
                allowed, reason = True, "device_cookie"
        return {
            "signed_in": True,
            "has_access": allowed,
            "reason": reason,
            "is_superadmin": bool(full.get("is_superadmin")),
        }

    # ── Public: my access state ───────────────────────────────────────
    @r.get("/api/access-codes/me")
    async def my_access_state(u: dict = Depends(get_current_user)):
        """Detailed view of the SIGNED-IN user's access window — used
        by the TopBar pill, the Billing-page "Extend access" panel,
        and the read-only banner. Returns:

            {
              "mode": "full" | "readonly" | "blocked",
              "reason": str,
              "expires_at": ISO | null,
              "days_remaining": int | null,
              "in_grace": bool,
              "grace_days_remaining": int | null,
              "is_perpetual": bool
            }

        Distinct from `/status` (which is a yes/no gate decision) —
        this endpoint is only called once the gate has already opened
        so it can assume the user is signed in.
        """
        try:
            full = await db.users.find_one({"_id": ObjectId(u["id"])}) or u
        except Exception:
            full = u
        return access_state(full)

    # ── Public: validate (no redeem) ──────────────────────────────────
    @r.post("/api/access-codes/validate")
    async def validate(body: RedeemBody):
        """Lightweight check for the registration form. Lets the SPA
        confirm a code is valid BEFORE it commits to creating a
        Firebase account, so a bad code doesn't leave the user with
        a half-baked auth state. Returns ``{ok: true, kind}`` on
        success, raises 400 otherwise."""
        result = await validate_code_only(db, body.code)
        if not result:
            raise HTTPException(
                400,
                "That code didn't check out — make sure it hasn't been "
                "used, revoked, or expired.",
            )
        kind, _row = result
        return {"ok": True, "kind": kind}

    # ── Public: redeem ────────────────────────────────────────────────
    @r.post("/api/access-codes/redeem")
    async def redeem(body: RedeemBody, request: Request, response: Response,
                     u: dict = Depends(get_current_user)):
        """Burn a code on behalf of the signed-in user. Accepts BOTH
        admin-issued private codes and the existing 24h trial codes.

        Trial-code branch: we don't change trial_codes here (the
        original trial.py owns that lifecycle for the registration
        funnel). We just mark the user as "access granted" if the
        code is currently valid for them. Same code can't be re-used
        as a private code after the trial window — that would let one
        leaked code grant perpetual access.
        """
        code = _norm_code(body.code)
        if not code:
            raise HTTPException(400, "Enter a code.")
        now = _now()
        try:
            user_obj_id = ObjectId(u["id"])
        except Exception:
            raise HTTPException(400, "Invalid user")
        full = await db.users.find_one({"_id": user_obj_id}) or {}

        # ── Branch 1: admin-issued private code ──
        row = await db.access_codes.find_one({"code": code})
        if row:
            if not row.get("is_active", True):
                raise HTTPException(400, "That code has been revoked.")
            expires = row.get("expires_at")
            if isinstance(expires, datetime):
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires < now:
                    raise HTTPException(400, "That code has expired.")
            # Per-user idempotency: if THIS user already redeemed
            # the SAME code, refuse the second redemption — without
            # this guard a user could click "redeem" twice and stack
            # the duration days for free.
            already_redeemed_by = row.get("redeemed_by_user_ids") or []
            already_used_this_code = str(user_obj_id) in already_redeemed_by
            duration_days = row.get("grants_duration_days")
            has_duration = isinstance(duration_days, int) and duration_days > 0

            # Already-allowed short-circuit: if the user is currently
            # green AND the code is perpetual, redeeming would be a
            # no-op — return the idempotent success that the SPA's
            # stale-gate paths rely on. But if the code DOES carry
            # a duration, fall through to the stacking branch so
            # active users can legitimately extend their window.
            already_allowed, reason = _user_has_access(full)
            if already_allowed and not has_duration:
                return {"ok": True, "reason": reason, "already_granted": True}

            if already_used_this_code:
                raise HTTPException(
                    400,
                    "You've already redeemed this code. Each code can "
                    "only be applied to your account once — ask for a "
                    "fresh one to extend further.",
                )
            # Cap checks for new redemptions.
            if row.get("usage_type") == "single":
                if (row.get("used_count") or 0) >= 1:
                    raise HTTPException(400, "That code has already been used.")
            else:  # multi
                cap = row.get("max_uses")
                if cap is not None and (row.get("used_count") or 0) >= cap:
                    raise HTTPException(400, "That code has reached its usage limit.")
            await db.access_codes.update_one(
                {"_id": row["_id"]},
                {
                    "$inc": {"used_count": 1},
                    "$addToSet": {"redeemed_by_user_ids": str(user_obj_id)},
                    "$set": {"last_used_at": now},
                },
            )
            # Stacking math — codes that carry `grants_duration_days`
            # extend the user's window from `max(current_expiry, now)`;
            # perpetual codes just stamp `access_granted_at`.
            user_updates: dict = {
                "access_granted_at": now,
                "access_code_used": code,
                "access_code_id": str(row["_id"]),
            }
            if has_duration:
                current_expiry = full.get("access_expires_at")
                if isinstance(current_expiry, datetime):
                    if current_expiry.tzinfo is None:
                        current_expiry = current_expiry.replace(tzinfo=timezone.utc)
                    base = max(current_expiry, now)
                else:
                    # Perpetual users redeeming a duration-bearing code:
                    # we DON'T clamp them down to a window — they keep
                    # their perpetual access. This guards against the
                    # "grandfathered user accidentally caps themselves
                    # at 30 days" footgun.
                    if already_allowed and not isinstance(current_expiry, datetime):
                        # Perpetual user — record the redemption but
                        # leave them perpetual.
                        await db.users.update_one(
                            {"_id": user_obj_id},
                            {"$set": user_updates},
                        )
                        set_access_cookie(response, mint_device_cookie(str(row["_id"])))
                        return {
                            "ok": True,
                            "reason": "private_code",
                            "extended_days": duration_days,
                            "new_expires_at": None,
                            "kept_perpetual": True,
                        }
                    base = now
                user_updates["access_expires_at"] = base + timedelta(days=duration_days)
                user_updates["access_extended_at"] = now
            await db.users.update_one(
                {"_id": user_obj_id},
                {"$set": user_updates},
            )
            set_access_cookie(response, mint_device_cookie(str(row["_id"])))
            new_expiry = user_updates.get("access_expires_at")
            return {
                "ok": True,
                "reason": "private_code",
                "extended_days": duration_days if has_duration else None,
                "new_expires_at": new_expiry.isoformat() if isinstance(new_expiry, datetime) else None,
            }

        # If the code wasn't an access code AND the user already has
        # access, surface a friendly "you're already green" message
        # rather than the misleading "code not found".
        already_allowed, reason = _user_has_access(full)
        if already_allowed:
            # Fall through to trial-code branch below — a user with
            # active access might still want to redeem a trial code,
            # but trial codes don't stack. So if we hit the trial
            # branch and they're already allowed we just no-op.
            pass

        # ── Branch 2: trial code (24h trial system) ──
        # We accept active, unredeemed trial codes here too so users
        # who somehow registered without going through the trial funnel
        # can still unlock with one. We mark the trial code spent (so
        # the same code can't double-up as both registration ticket and
        # post-hoc unlock).
        trial_row = await db.trial_codes.find_one({"code": code})
        if trial_row:
            if already_allowed:
                # User already has access — trial codes don't stack
                # (24h flat). Refuse with a useful pointer to a real
                # extension code.
                raise HTTPException(
                    400,
                    "You already have active access — trial codes are "
                    "for first-time activation only. Ask an admin for a "
                    "duration-bearing access code to extend.",
                )
            if trial_row.get("used_at"):
                raise HTTPException(400, "That code has already been redeemed.")
            t_expires = trial_row.get("expires_at")
            if isinstance(t_expires, datetime):
                if t_expires.tzinfo is None:
                    t_expires = t_expires.replace(tzinfo=timezone.utc)
                if t_expires < now:
                    raise HTTPException(400, "That code has expired.")
            # Bind the trial timer onto this user (24h from now).
            trial_started_at = now
            trial_expires_at = now + timedelta(hours=24)
            await db.users.update_one(
                {"_id": user_obj_id},
                {"$set": {
                    "access_granted_at": now,
                    "access_code_used": code,
                    "trial_started_at": trial_started_at,
                    "trial_expires_at": trial_expires_at,
                    "trial_source": trial_row.get("source") or "post_signup_redeem",
                }},
            )
            await db.trial_codes.update_one(
                {"_id": trial_row["_id"]},
                {"$set": {"used_at": now, "used_by_user_id": str(user_obj_id)}},
            )
            # Trial cookie uses the trial-code id as the device proof.
            set_access_cookie(response, mint_device_cookie(str(trial_row["_id"])))
            return {"ok": True, "reason": "trial"}

        raise HTTPException(404, "We couldn't find that code. Double-check it and try again.")

    # ── Admin: list codes ─────────────────────────────────────────────
    @r.get("/api/admin/access-codes")
    async def admin_list_codes(
        _: dict = Depends(require_superadmin),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
    ):
        total = await db.access_codes.count_documents({})
        cur = (
            db.access_codes
            .find({})
            .sort("created_at", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        rows = []
        async for r_ in cur:
            expires = r_.get("expires_at")
            rows.append({
                "id": str(r_["_id"]),
                "code": r_.get("code") or "",
                "usage_type": r_.get("usage_type") or "single",
                "max_uses": r_.get("max_uses"),
                "used_count": r_.get("used_count") or 0,
                "is_active": bool(r_.get("is_active", True)),
                "expires_at": expires.isoformat() if isinstance(expires, datetime) else None,
                "grants_duration_days": r_.get("grants_duration_days"),
                "created_at": r_["created_at"].isoformat()
                    if isinstance(r_.get("created_at"), datetime) else None,
                "last_used_at": r_["last_used_at"].isoformat()
                    if isinstance(r_.get("last_used_at"), datetime) else None,
                "note": r_.get("note") or "",
                "created_by": r_.get("created_by") or "",
                "redeemed_by_user_ids": r_.get("redeemed_by_user_ids") or [],
            })
        return {"page": page, "page_size": page_size, "total": total, "rows": rows}

    # ── Admin: create code ────────────────────────────────────────────
    @r.post("/api/admin/access-codes")
    async def admin_create_code(body: CreateCodeBody,
                                admin: dict = Depends(require_superadmin)):
        now = _now()
        code = _norm_code(body.code) if body.code else _generate_code()
        if len(code) < 4:
            raise HTTPException(400, "Code must be at least 4 characters.")
        # Avoid colliding with an existing access code OR an existing
        # trial code — both pools are honoured at redeem time.
        if await db.access_codes.find_one({"code": code}):
            raise HTTPException(409, "A code with that value already exists.")
        if await db.trial_codes.find_one({"code": code}):
            raise HTTPException(409, "That code clashes with an existing trial code — pick another.")
        expires_at = None
        if body.expires_at:
            try:
                expires_at = datetime.fromisoformat(body.expires_at.replace("Z", "+00:00"))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            except Exception:
                raise HTTPException(400, "Invalid expires_at — must be ISO datetime.")
        max_uses = body.max_uses if body.usage_type == "multi" else None
        doc = {
            "code": code,
            "usage_type": body.usage_type,
            "max_uses": max_uses,
            "used_count": 0,
            "is_active": True,
            "expires_at": expires_at,
            "grants_duration_days": body.grants_duration_days,
            "note": (body.note or "").strip(),
            "created_at": now,
            "created_by": admin.get("id") or "",
            "redeemed_by_user_ids": [],
        }
        res = await db.access_codes.insert_one(doc)
        return {
            "id": str(res.inserted_id),
            "code": code,
            "usage_type": doc["usage_type"],
            "max_uses": doc["max_uses"],
            "used_count": 0,
            "is_active": True,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "grants_duration_days": doc["grants_duration_days"],
            "created_at": now.isoformat(),
            "note": doc["note"],
        }

    # ── Admin: revoke code ────────────────────────────────────────────
    @r.delete("/api/admin/access-codes/{code_id}")
    async def admin_revoke_code(code_id: str,
                                _: dict = Depends(require_superadmin)):
        try:
            oid = ObjectId(code_id)
        except Exception:
            raise HTTPException(400, "Invalid code id")
        result = await db.access_codes.update_one(
            {"_id": oid}, {"$set": {"is_active": False, "revoked_at": _now()}}
        )
        if result.matched_count == 0:
            raise HTTPException(404, "Code not found")
        return {"ok": True}

    return r


__all__ = ["make_access_codes_router", "_user_has_access",
           "device_cookie_grants_access",
           "validate_code_only", "redeem_code_for_user",
           "grandfather_existing_users",
           "mint_device_cookie", "set_access_cookie",
           "ACCESS_COOKIE_NAME", "ACCESS_COOKIE_MAX_AGE",
           "access_state", "assert_full_access", "ACCESS_GRACE_DAYS"]
