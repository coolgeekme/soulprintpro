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
        "superadmin" / "private_code" / "trial" / "none"
    """
    if not user:
        return False, "none"
    if user.get("is_superadmin"):
        return True, "superadmin"
    if user.get("access_granted_at"):
        return True, "private_code"
    # Active or even past-but-redeemed trial counts — once you've
    # touched the trial flow, we don't slam the door behind you.
    if user.get("trial_started_at"):
        return True, "trial"
    return False, "none"


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
        await db.users.update_one(
            {"_id": user_obj_id},
            {"$set": {
                "access_granted_at": now,
                "access_code_used": code,
                "access_code_id": str(row["_id"]),
            }},
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
    # ISO datetime string. None = never expires.
    expires_at: Optional[str] = None
    note: Optional[str] = Field(None, max_length=240)


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

        # If they already have access, redeeming is a no-op success —
        # avoids weird "you already used this" errors when the SPA
        # re-posts on a stale gate render.
        already_allowed, reason = _user_has_access(full)
        if already_allowed:
            return {"ok": True, "reason": reason, "already_granted": True}

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
            # Per-user idempotency — if THIS user already redeemed
            # the same code we just stamp them green again.
            already_redeemed_by = row.get("redeemed_by_user_ids") or []
            if str(user_obj_id) in already_redeemed_by:
                pass  # idempotent — fall through to the granting block
            else:
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
            await db.users.update_one(
                {"_id": user_obj_id},
                {"$set": {
                    "access_granted_at": now,
                    "access_code_used": code,
                    "access_code_id": str(row["_id"]),
                }},
            )
            set_access_cookie(response, mint_device_cookie(str(row["_id"])))
            return {"ok": True, "reason": "private_code"}

        # ── Branch 2: trial code (24h trial system) ──
        # We accept active, unredeemed trial codes here too so users
        # who somehow registered without going through the trial funnel
        # can still unlock with one. We mark the trial code spent (so
        # the same code can't double-up as both registration ticket and
        # post-hoc unlock).
        trial_row = await db.trial_codes.find_one({"code": code})
        if trial_row:
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
           "redeem_code_for_user", "validate_code_only",
           "grandfather_existing_users",
           "mint_device_cookie", "set_access_cookie",
           "ACCESS_COOKIE_NAME", "ACCESS_COOKIE_MAX_AGE"]
