"""Trial code system — request-then-redeem self-serve trial.

Flow:
  1. Visitor (typically from the audit page) requests a trial code by
     submitting their email.
  2. We mail them a one-time code via Resend.
  3. They register a real account at any time using that code. The
     code does TWO things at once:
        * verifies their email (it was just delivered to that inbox)
        * starts their 24-hour trial clock from the moment of redemption
  4. Their user row gets `trial_started_at` and `trial_expires_at`.
     The frontend reads these and shows a countdown banner.

Why this shape?
  * Email-verification + clock-start collapsed into a single step. Saves
    a registration page from being a multi-stage friction trap.
  * The clock starts when the prospect chooses to start it, not when
    they fill in the email. A founder who clicks the audit on Friday
    afternoon won't have a dead trial Monday morning.
  * The code is single-use and tied to that email, so it can't be
    forwarded or screen-shotted around.

We DON'T gate features on `trial_status == "active"` yet — that's a
follow-on once billing exists. For now the trial is a soft-state
flag we can pivot on later.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

logger = logging.getLogger(__name__)


# Tunables
TRIAL_DURATION_HOURS = 24
TRIAL_CODE_TTL_HOURS = 14 * 24   # codes expire after 14 days unredeemed
TRIAL_CODE_LENGTH = 8            # human-friendly, alphanumeric


def _new_code() -> str:
    """Generate a friendly code: 8 chars from a non-ambiguous alphabet
    (no 0/O/1/l/I — easy to read off a phone email and type without
    typos). 8 chars × 28 alphabet ≈ 3.7e11 combos, plenty for the
    14-day TTL."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(TRIAL_CODE_LENGTH))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Public API models ──────────────────────────────────────────────────────


class TrialRequestBody(BaseModel):
    email: EmailStr
    name: Optional[str] = Field(None, max_length=80)
    # Where the visitor came from — purely for funnel analytics. The
    # audit page will pass its slug so we can later count audit→trial
    # conversion rates per audit.
    source: Optional[str] = Field(None, max_length=120)


class TrialRedeemBody(BaseModel):
    email: EmailStr
    code: str = Field(..., min_length=4, max_length=24)
    name: str = Field(..., min_length=1, max_length=80)
    password: str = Field(..., min_length=8, max_length=200)


# ── Email helper (Resend) ──────────────────────────────────────────────────


async def _send_trial_code_email(*, to: str, name: str, code: str,
                                 origin: str) -> bool:
    """POST a trial-code email to Resend. Best-effort — failure is
    logged but the code row is still persisted so the user can request
    it again or copy from the response payload (we ALSO return the code
    in the API response in dev so testing doesn't depend on email
    delivery — see route handler).
    """
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("RESEND_FROM", "").strip()
    if not api_key or not sender:
        logger.warning(
            "Resend not configured (RESEND_API_KEY=%s, RESEND_FROM=%s) — "
            "trial code email will NOT be delivered for %s",
            "set" if api_key else "MISSING",
            "set" if sender else "MISSING",
            to,
        )
        return False
    redeem_url = f"{origin.rstrip('/')}/trial/redeem?code={code}"
    subject = "Your SoulPrint Engine Pro trial code"
    text = (
        f"Hi {name or 'there'},\n\n"
        f"Your SoulPrint Engine Pro 24-hour trial is ready when you are.\n\n"
        f"Your code: {code}\n\n"
        f"Or click here to start now:\n{redeem_url}\n\n"
        f"The clock starts the moment you redeem the code — not now. So "
        f"go grab a coffee, get your team together, and come back when you're "
        f"ready to put us through our paces.\n\n"
        f"This code is good for 14 days.\n"
    )
    html = (
        f"<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        f"max-width:520px;margin:0 auto;padding:24px;color:#222'>"
        f"<p style='font-size:13px;color:#888;text-transform:uppercase;"
        f"letter-spacing:0.1em;margin:0 0 8px'>Trial code</p>"
        f"<h2 style='font-size:22px;margin:0 0 16px;line-height:1.3'>"
        f"Your SoulPrint Engine Pro 24-hour trial is ready when you are.</h2>"
        f"<p style='font-size:14px;line-height:1.6;margin:0 0 8px'>"
        f"Hi {name or 'there'},</p>"
        f"<p style='font-size:14px;line-height:1.6;margin:0 0 16px'>"
        f"The clock starts the moment you redeem the code — not now. "
        f"So go grab a coffee, get your team together, and come back when "
        f"you're ready to put us through our paces.</p>"
        f"<div style='background:#fff7ed;border:2px dashed #f97316;"
        f"border-radius:12px;padding:16px;text-align:center;margin:0 0 16px'>"
        f"<div style='font-size:12px;color:#9a3412;letter-spacing:0.15em;"
        f"text-transform:uppercase;margin-bottom:6px'>Your code</div>"
        f"<div style='font-family:monospace;font-size:28px;font-weight:600;"
        f"letter-spacing:0.15em;color:#9a3412'>{code}</div></div>"
        f"<div style='margin:24px 0'>"
        f"<a href='{redeem_url}' style='display:inline-block;"
        f"background:#f97316;color:#fff;padding:12px 22px;border-radius:999px;"
        f"text-decoration:none;font-weight:600;font-size:14px'>"
        f"Start my 24-hour trial</a></div>"
        f"<p style='font-size:12px;color:#888;margin:16px 0 0'>"
        f"Or paste this link into your browser: "
        f"<a href='{redeem_url}' style='color:#f97316'>{redeem_url}</a></p>"
        f"<p style='font-size:12px;color:#888;margin:8px 0 0'>"
        f"This code is good for 14 days.</p>"
        f"</div>"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"from": sender, "to": [to], "subject": subject,
                      "html": html, "text": text},
            )
        if resp.status_code >= 300:
            logger.error(
                "TRIAL_EMAIL_FAILED resend_status=%s recipient=%s sender=%s body=%s",
                resp.status_code, to, sender, resp.text[:400],
            )
            return False
        return True
    except Exception as exc:
        logger.error("TRIAL_EMAIL_EXCEPTION recipient=%s sender=%s: %s",
                     to, sender, exc)
        return False


# ── Router factory ─────────────────────────────────────────────────────────


def make_trial_router(*, db, set_auth_cookies, create_access_token,
                      public_origin) -> APIRouter:
    """Build the FastAPI router. `set_auth_cookies` and
    `create_access_token` come from server.py so we can sign the user
    in immediately on redeem — same UX as the regular login form.
    `public_origin(request)` returns the correct frontend origin for
    the redeem URL in the email body.
    """
    r = APIRouter(prefix="/api/trial")

    @r.post("/request-code")
    async def request_code(body: TrialRequestBody, request: Request):
        """Issue a trial code and email it. Idempotent within the TTL —
        re-requesting from the same email upserts the same code so the
        prospect doesn't get a wall of inbox emails if they double-click.
        """
        email = body.email.lower().strip()
        # If they already have an active account, send them to login
        # instead of creating another trial — preserves the integrity
        # of the "one trial per person" promise.
        existing_user = await db.users.find_one({"email": email})
        if existing_user:
            return {
                "ok": False,
                "already_registered": True,
                "detail": ("That email already has a SoulPrint Engine "
                           "account. Sign in instead — we'll show your "
                           "trial status from there."),
            }
        now = _now()
        # Reuse a pending code if one already exists and isn't expired.
        existing_code = await db.trial_codes.find_one({
            "email": email,
            "used_at": None,
            "expires_at": {"$gt": now},
        })
        if existing_code:
            code = existing_code["code"]
            # Don't bombard them — only resend if the previous email
            # is older than 60 seconds.
            last_sent = existing_code.get("last_sent_at") or now
            if isinstance(last_sent, datetime):
                if last_sent.tzinfo is None:
                    last_sent = last_sent.replace(tzinfo=timezone.utc)
                if (now - last_sent).total_seconds() < 60:
                    return {"ok": True, "already_sent": True}
        else:
            code = _new_code()
            await db.trial_codes.insert_one({
                "email": email, "code": code,
                "name": (body.name or "").strip(),
                "source": (body.source or "").strip(),
                "created_at": now,
                "expires_at": now + timedelta(hours=TRIAL_CODE_TTL_HOURS),
                "last_sent_at": None,
                "used_at": None, "used_by_user_id": None,
                "request_ip": (request.client.host if request.client else ""),
            })
        # Send the email (best-effort).
        sent = await _send_trial_code_email(
            to=email,
            name=(body.name or "").strip() or email.split("@")[0],
            code=code,
            origin=public_origin(request),
        )
        await db.trial_codes.update_one(
            {"email": email, "code": code},
            {"$set": {"last_sent_at": _now()}},
        )
        out = {"ok": True, "email_sent": sent}
        # In environments without Resend configured, expose the code so
        # we don't soft-fail. Production will have RESEND_API_KEY set.
        if not sent:
            out["dev_code"] = code
        return out

    @r.post("/redeem")
    async def redeem_code(body: TrialRedeemBody, request: Request,
                          response: Response):
        """Validate the code, create the user account, start the 24h clock,
        sign them in. Returns the same shape as /api/auth/login so the
        frontend can drop the response into AuthContext as-is.
        """
        email = body.email.lower().strip()
        code = body.code.upper().strip()
        now = _now()
        row = await db.trial_codes.find_one({"email": email, "code": code})
        if not row:
            raise HTTPException(404, "We couldn't find that code. Double-check "
                                     "the email and try again.")
        if row.get("used_at"):
            raise HTTPException(400, "That code has already been redeemed.")
        expires = row.get("expires_at")
        if isinstance(expires, datetime):
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < now:
                raise HTTPException(400, "That code expired. Request a fresh one.")
        # If the email shockingly raced into a real user row between
        # request-code and redeem, refuse. Better to confuse one
        # well-meaning user than to silently overwrite an existing
        # account row.
        existing_user = await db.users.find_one({"email": email})
        if existing_user:
            raise HTTPException(409, "That email already has an account. "
                                     "Sign in instead.")
        # Create the user. Trial fields are stamped right on the user
        # row so subsequent /api/auth/me calls can return the trial
        # state without an extra query.
        password_hash = bcrypt.hashpw(
            body.password.encode("utf-8"), bcrypt.gensalt(),
        ).decode("utf-8")
        trial_started_at = now
        trial_expires_at = now + timedelta(hours=TRIAL_DURATION_HOURS)
        user_doc = {
            "name": body.name.strip(),
            "email": email,
            "email_verified_at": now,    # the code WAS the verification
            "password_hash": password_hash,
            "is_superadmin": False,
            "is_disabled": False,
            "created_at": now,
            "trial_started_at": trial_started_at,
            "trial_expires_at": trial_expires_at,
            "trial_source": row.get("source") or "",
            # Trial redemption counts as a code redemption for the
            # pre-launch site access gate — so the user doesn't bounce
            # off the gate after their email-verified trial sign-up.
            "access_granted_at": now,
            "access_code_used": code,
        }
        result = await db.users.insert_one(user_doc)
        uid = str(result.inserted_id)
        # Mark the code spent.
        await db.trial_codes.update_one(
            {"_id": row["_id"]},
            {"$set": {"used_at": now, "used_by_user_id": uid}},
        )
        # Mint a session token and stamp the auth cookie so the SPA
        # can redirect to /app fully signed in.
        token = create_access_token(uid, email)
        set_auth_cookies(response, token)
        return {
            "id": uid, "name": body.name.strip(), "email": email,
            "is_superadmin": False,
            "access_token": token,
            "trial_started_at": trial_started_at.isoformat(),
            "trial_expires_at": trial_expires_at.isoformat(),
        }

    @r.get("/status")
    async def trial_status(request: Request):
        """Lookup helper for the frontend — returns the caller's trial
        state. Public-safe (no auth required) so the audit page can
        deep-link into a "you already have a trial" branch without
        forcing a login round-trip first."""
        # Reads the auth cookie if present; falls back to anonymous.
        from server import get_user_or_none  # local to avoid bootstrap cycle
        u = await get_user_or_none(request)
        if not u:
            return {"signed_in": False}
        started = u.get("trial_started_at")
        expires = u.get("trial_expires_at")
        if not started or not expires:
            return {"signed_in": True, "has_trial": False}
        if isinstance(expires, datetime) and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if isinstance(started, datetime) and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        active = bool(expires and expires > _now())
        return {
            "signed_in": True,
            "has_trial": True,
            "trial_active": active,
            "trial_started_at": started.isoformat() if started else None,
            "trial_expires_at": expires.isoformat() if expires else None,
        }

    return r


__all__ = ["make_trial_router", "TRIAL_DURATION_HOURS"]
