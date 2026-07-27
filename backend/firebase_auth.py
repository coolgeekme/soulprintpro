"""Firebase ID-token verification — JWKS-based, no Admin SDK required.

Keeps MongoDB Atlas as the source of truth for users, orgs, etc. Firebase
is only the sign-in layer: it issues an ID token, the frontend sends it
on every API request as `Authorization: Bearer <idToken>`, and this
module verifies it against Google's public certificates.

On first sign-in we either (a) link the Firebase UID onto an existing
Mongo user that matches by email — preserving their orgs / role /
history — or (b) auto-create a fresh Mongo user record. Either way the
caller of `get_current_user_hybrid` receives the same shape it received
from the legacy JWT path.

Why not Firebase Admin SDK?
- The user's GCP org policy blocks downloading service-account keys.
- ID-token verification is fully spec'd over Google's public JWKS — no
  private credentials needed.
- Roles / claims live in Mongo (existing schema). We don't need
  `set_custom_user_claims` because the backend looks up the user record
  on every request anyway.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────
# The Firebase project id this backend trusts. Tokens issued by any other
# project (or no project) will be rejected. Pulled from the same env var
# the frontend uses, so a single source of truth.
FIREBASE_PROJECT_ID = (
    os.environ.get("FIREBASE_PROJECT_ID")
    or os.environ.get("REACT_APP_FIREBASE_PROJECT_ID")
    or ""
)

# Google publishes signing certs as JWKS at this URL — rotated every
# ~24h. PyJWKClient handles caching + rotation transparently.
GOOGLE_JWKS_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
# Modern JWK Set endpoint (used as primary). The x509 endpoint above is
# kept as a fallback because some networks block one or the other.
GOOGLE_JWKS_URL_MODERN = "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"

ISSUER_BASE = "https://securetoken.google.com/"

# Module-level JWKS client. Lazily initialised; PyJWKClient handles
# fetching and short-term caching of signing keys internally.
_jwk_client: Optional[PyJWKClient] = None
_jwks_cache: Optional[dict] = None
_jwks_fetched_at: float = 0.0
_JWKS_TTL_S = 3600  # 1h, matches Google's published cache headers


def firebase_enabled() -> bool:
    """True iff the backend has enough config to verify Firebase tokens.

    A bare project id is enough — no private credentials required.
    """
    return bool(FIREBASE_PROJECT_ID)


def _get_jwk_client() -> PyJWKClient:
    """Singleton PyJWKClient pointed at Google's modern JWK endpoint."""
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(GOOGLE_JWKS_URL_MODERN, cache_keys=True, lifespan=3600)
    return _jwk_client


def _looks_like_firebase_token(token: str) -> bool:
    """Cheap heuristic: a Firebase ID token is a JWT whose unverified
    `iss` claim points at `https://securetoken.google.com/<project_id>`.
    Returns False for our legacy HS256 tokens (which have a different
    issuer / no issuer at all)."""
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
        iss = unverified.get("iss") or ""
        return iss.startswith(ISSUER_BASE)
    except Exception:
        return False


def verify_firebase_id_token(token: str) -> dict:
    """Verify a Firebase ID token and return the decoded claims dict.

    Raises ValueError on any verification failure — caller maps to 401.
    """
    if not FIREBASE_PROJECT_ID:
        raise ValueError("Firebase verification is not configured (FIREBASE_PROJECT_ID missing)")

    expected_iss = f"{ISSUER_BASE}{FIREBASE_PROJECT_ID}"
    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token).key
    except Exception as exc:  # network blip, key rotation, malformed token
        # Re-init client once to recover from a stale cache and try again.
        global _jwk_client
        _jwk_client = None
        try:
            signing_key = _get_jwk_client().get_signing_key_from_jwt(token).key
        except Exception as exc2:
            raise ValueError(f"Could not fetch Firebase signing key: {exc2}") from exc

    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=FIREBASE_PROJECT_ID,
            issuer=expected_iss,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ValueError("Firebase token expired") from exc
    except jwt.InvalidAudienceError as exc:
        # Surface what the token's audience was vs what we expected so the
        # user knows when they're hitting the project-id mismatch trap
        # (most common cause: stale frontend bundle from a previous
        # Firebase project still cached in the browser).
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
            actual_aud = unverified.get("aud")
            actual_iss = unverified.get("iss")
        except Exception:
            actual_aud = "unknown"
            actual_iss = "unknown"
        raise ValueError(
            f"Audience mismatch — token aud={actual_aud!r}, expected {FIREBASE_PROJECT_ID!r}. "
            f"Token issuer={actual_iss!r}. "
            f"Likely cause: browser is running a cached frontend bundle from a different Firebase project. "
            f"Hard-refresh the page (Cmd/Ctrl+Shift+R) and clear site data."
        ) from exc
    except jwt.InvalidIssuerError as exc:
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
            actual_iss = unverified.get("iss")
        except Exception:
            actual_iss = "unknown"
        raise ValueError(
            f"Issuer mismatch — token iss={actual_iss!r}, expected {expected_iss!r}. "
            f"Hard-refresh the page and clear site data."
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise ValueError(f"Invalid Firebase token: {exc}") from exc

    # Firebase ID tokens additionally include an `auth_time` claim which
    # must be in the past — Google's docs require this check.
    auth_time = claims.get("auth_time")
    if isinstance(auth_time, (int, float)) and auth_time > time.time() + 30:
        raise ValueError("Firebase token has future auth_time")

    if not claims.get("sub"):
        raise ValueError("Firebase token has no subject (uid)")

    return claims


# ── Mongo upsert / link by email ─────────────────────────────────────


async def upsert_user_for_firebase_claims(db, claims: dict) -> dict:
    """Find or create the Mongo user record that backs a verified
    Firebase identity. Returns the user dict in the same shape that
    `get_current_user` returns.

    Linking rules:
    - If a user already exists with `firebase_uid == claims.sub`, return it.
    - Else if a user exists with the same email, attach the Firebase UID
      to that record (so existing legacy users keep their orgs / role /
      history when they first sign in via Google or Firebase password).
    - Else create a fresh user. Email + name are taken from the Firebase
      claims; no password hash is stored (Firebase manages the credential).
    """
    from bson import ObjectId  # local import — avoid circular pull at import time

    uid = str(claims.get("sub") or claims.get("user_id") or "")
    email = (claims.get("email") or "").strip().lower()
    name = (
        claims.get("name")
        or claims.get("displayName")
        or (email.split("@")[0] if email else "")
        or "User"
    )
    if not uid:
        raise ValueError("Firebase claims missing subject")

    # 1. Match by stored firebase_uid (already linked)
    existing = await db.users.find_one({"firebase_uid": uid})
    if existing:
        return _serialize_user(existing)

    # 2. Match by email (legacy user — link Firebase to it)
    if email:
        legacy = await db.users.find_one({"email": email})
        if legacy:
            await db.users.update_one(
                {"_id": legacy["_id"]},
                {"$set": {
                    "firebase_uid": uid,
                    "firebase_provider": _extract_provider(claims),
                    "firebase_linked_at": datetime.now(timezone.utc),
                    # Refresh display name only if it was previously empty
                    **({"name": name} if not legacy.get("name") else {}),
                }},
            )
            legacy = await db.users.find_one({"_id": legacy["_id"]})
            return _serialize_user(legacy)

    # 3. Brand-new account — auto-provision a Mongo record. No password
    #    hash; Firebase owns the credential. We DO NOT auto-promote any
    #    role — superadmin is opt-in via the existing SUPERADMIN_EMAILS
    #    env var which is checked on the legacy register path; a Firebase
    #    sign-in by a superadmin email gets the same treatment.
    superadmin_emails = {
        e.strip().lower()
        for e in (os.environ.get("SUPERADMIN_EMAILS") or "").split(",")
        if e.strip()
    }
    is_superadmin = email in superadmin_emails
    now = datetime.now(timezone.utc)
    res = await db.users.insert_one({
        "email": email,
        "name": name,
        "password_hash": "",   # Firebase-only — legacy login path won't work
        "firebase_uid": uid,
        "firebase_provider": _extract_provider(claims),
        "firebase_linked_at": now,
        "is_superadmin": is_superadmin,
        "created_at": now,
    })
    fresh = await db.users.find_one({"_id": res.inserted_id})
    # First-time Firebase signup → fire welcome email + team notify.
    # Best-effort; same fire-and-forget pattern as the legacy register
    # path so a flaky Resend doesn't break Google-sign-in flows. Team
    # notification is skipped for staff emails (founders signing in).
    try:
        import asyncio as _asyncio
        from email_service import send_welcome_email
        try:
            from staff import is_staff_email
            notify_team = not is_staff_email(email)
        except Exception:
            notify_team = True
        _asyncio.create_task(send_welcome_email(
            name=name, email=email, notify_team=notify_team,
        ))
    except Exception:
        pass
    return _serialize_user(fresh)


def _serialize_user(u: dict) -> dict:
    """Match the shape returned by the legacy `get_current_user`."""
    email = u.get("email", "")
    is_super = bool(u.get("is_superadmin"))
    is_priv = is_super
    if not is_priv:
        try:
            from staff import is_staff_email
            is_priv = is_staff_email(email)
        except Exception:
            is_priv = False
    return {
        "id": str(u["_id"]),
        "email": email,
        "name": u.get("name", "") or "User",
        "is_superadmin": is_super,
        "is_privileged": bool(is_priv),
    }


def _extract_provider(claims: dict) -> str:
    """Cheap label for which Firebase provider was used — useful for
    audit logs / 'sign-in method' UI later."""
    fb = claims.get("firebase") or {}
    sign_in = fb.get("sign_in_provider") or ""
    return sign_in or ("password" if claims.get("email_verified") is False else "unknown")
