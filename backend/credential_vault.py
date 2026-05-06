"""Per-domain credential vault for ChatGPT Agent Mode.

After a user takes control of a mission's remote browser and authenticates
into a site (Google, LinkedIn, Stripe, banking, etc.), we capture the
Chromium context's `storage_state` (cookies + localStorage), encrypt it
with Fernet, and persist it per-(org, hostname). On any subsequent
mission whose `_get_or_create_mission_page` is touching the same host,
we transparently inject those cookies — so the user logs in ONCE per
site, not once per mission.

Encryption:
  • Fernet (symmetric AES-128-CBC + HMAC-SHA256) via `cryptography`.
  • Key sourced from `SOULPRINT_VAULT_KEY` env var. If absent on first
    boot we generate a per-pod key and log a loud warning — the operator
    SHOULD set a stable one in `backend/.env` so cookies persist across
    pod restarts. Without it the cookies become unreadable on redeploy
    (we just delete the unreadable rows on read).

Schema (`domain_credentials` collection):
  {
    _id, org_id, hostname,                 # primary key (org_id, hostname)
    storage_state_encrypted: bytes,        # Fernet ciphertext
    updated_at, created_by,
    last_used_at, mission_count,
    label,                                 # human label e.g. "Google Workspace"
  }

Hostname normalisation: every URL we see is reduced to its eTLD+1
(e.g. mail.google.com → google.com, accounts.linkedin.com → linkedin.com)
so a single login covers every subdomain of the same site.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


_FERNET: Optional[Fernet] = None


def _fernet() -> Fernet:
    global _FERNET
    if _FERNET is not None:
        return _FERNET
    key = (os.environ.get("SOULPRINT_VAULT_KEY") or "").strip()
    if not key:
        key = Fernet.generate_key().decode()
        logger.warning(
            "SOULPRINT_VAULT_KEY not set — generated an EPHEMERAL key for "
            "this pod. Saved credentials will be UNREADABLE after a "
            "redeploy. Set SOULPRINT_VAULT_KEY=<fernet-key> in backend/.env "
            "for persistence."
        )
    try:
        _FERNET = Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as exc:
        logger.error("Invalid SOULPRINT_VAULT_KEY (%s) — generating new", exc)
        _FERNET = Fernet(Fernet.generate_key())
    return _FERNET


_PUBLIC_SUFFIXES = {
    # Common 2-level public suffixes — extend as needed. We don't pull
    # the full Mozilla list (~10k entries) since 99% of business cases
    # land on .com / .net / .org / .co.uk / .com.au.
    "co.uk", "ac.uk", "gov.uk", "org.uk",
    "com.au", "net.au", "org.au",
    "co.nz", "co.za", "co.in", "com.br",
}


def host_to_etld_plus_one(host: str) -> str:
    """Return the registrable domain for a hostname (eTLD+1)."""
    host = (host or "").strip().lower().split(":")[0]
    if not host or "." not in host:
        return host
    parts = host.split(".")
    if len(parts) >= 3:
        last2 = ".".join(parts[-2:])
        if last2 in _PUBLIC_SUFFIXES:
            return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def url_to_etld_plus_one(url: str) -> str:
    """Pull the eTLD+1 out of any URL, falling back gracefully."""
    if not url:
        return ""
    try:
        u = urlparse(url if "://" in url else f"https://{url}")
        return host_to_etld_plus_one(u.hostname or "")
    except Exception:
        return ""


# ── Login-wall detection ──────────────────────────────────────────────────
# Pages whose URL matches any of these patterns are treated as "user must
# log in". When the agent's browser lands on one of these URLs we:
#   1. set `mission.needs_login = <hostname>`
#   2. set `mission.user_controls_browser = True` (pauses the loop)
#   3. append a `system` event so the work log shows the pause reason
# The user then opens the live browser, types their password, and clicks
# "Hand back to agent" — at which point we record the login (B4) and
# the loop resumes.
_LOGIN_URL_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"https?://accounts\.google\.com/",
        r"https?://login\.live\.com/",
        r"https?://login\.microsoftonline\.com/",
        r"https?://www\.linkedin\.com/(login|checkpoint)",
        r"https?://login\.linkedin\.com/",
        r"https?://twitter\.com/i/flow/login",
        r"https?://x\.com/i/flow/login",
        r"https?://www\.facebook\.com/login",
        r"https?://github\.com/login",
        r"https?://www\.instagram\.com/accounts/login",
        r"https?://www\.tiktok\.com/login",
        r"https?://(www\.)?reddit\.com/login",
        r"https?://(?:www\.)?dashboard\.stripe\.com/login",
        r"https?://(?:secure\.)?paypal\.com/(signin|webapps/mpp/account-selection)",
        r"https?://signin\.aws\.amazon\.com/",
        r"https?://(?:[a-z0-9.-]+)?okta\.com/(signin|login)",
        r"https?://(?:[a-z0-9.-]+)?auth0\.com/(login|u/login)",
        r"/(login|signin|sign[-_]?in|auth/login)(?:[/?#]|$)",
    )
]


# Page-content markers that strongly suggest a login wall even when the
# URL itself doesn't match (e.g. a site that puts a login form on its
# regular path). Triggered only when the rendered text is short — a
# real article that mentions "sign in" doesn't count.
_LOGIN_TEXT_MARKERS = (
    "sign in to your account",
    "log in to continue",
    "please log in",
    "you need to sign in",
    "sign in with google",
    "continue with apple",
    "two-factor authentication required",
    "verify your identity",
    "authentication required",
)


def looks_like_login_page(url: str, title: str = "", text: str = "") -> bool:
    """Heuristic: does this page require interactive auth?"""
    url = url or ""
    title = (title or "").lower()
    text = (text or "").lower()
    if any(p.search(url) for p in _LOGIN_URL_PATTERNS):
        return True
    if any(m in title for m in ("sign in", "log in", "login", "authenticate")) and len(text) < 4000:
        return True
    if any(m in text for m in _LOGIN_TEXT_MARKERS) and len(text) < 4000:
        return True
    return False


# ── Vault CRUD ─────────────────────────────────────────────────────────────
async def save_credentials(
    db, *, org_id: str, hostname: str, storage_state: Dict[str, Any],
    user_id: str = "", label: str = "",
) -> str:
    """Encrypt and upsert a storage_state for (org_id, hostname).

    Returns the hostname (eTLD+1) we actually keyed under so the caller
    can show "Saved login for `google.com`" in the UI.
    """
    host = host_to_etld_plus_one(hostname)
    if not host:
        raise ValueError("invalid hostname")
    payload = json.dumps(storage_state, default=str).encode("utf-8")
    cipher = _fernet().encrypt(payload)
    now = datetime.now(timezone.utc)
    await db.domain_credentials.update_one(
        {"org_id": org_id, "hostname": host},
        {
            "$set": {
                "storage_state_encrypted": cipher,
                "updated_at": now,
                "label": label or host,
            },
            "$setOnInsert": {
                "org_id": org_id,
                "hostname": host,
                "created_by": user_id,
                "created_at": now,
                "mission_count": 0,
            },
        },
        upsert=True,
    )
    logger.info("Vault: saved credentials for %s/%s (%d bytes)", org_id, host, len(cipher))
    return host


async def load_credentials(
    db, *, org_id: str, hostname: str,
) -> Optional[Dict[str, Any]]:
    """Fetch + decrypt the storage_state for (org_id, hostname)."""
    host = host_to_etld_plus_one(hostname)
    if not host:
        return None
    row = await db.domain_credentials.find_one(
        {"org_id": org_id, "hostname": host},
    )
    if not row:
        return None
    cipher = row.get("storage_state_encrypted")
    if not cipher:
        return None
    try:
        plain = _fernet().decrypt(cipher)
        return json.loads(plain.decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Vault: failed to decrypt %s/%s (%s) — deleting row",
                       org_id, host, exc)
        await db.domain_credentials.delete_one({"_id": row["_id"]})
        return None


async def list_credentials(db, *, org_id: str) -> list[dict]:
    """Public-safe listing — never returns ciphertext, only metadata."""
    out = []
    cur = db.domain_credentials.find(
        {"org_id": org_id},
        {
            "hostname": 1, "label": 1, "updated_at": 1, "created_by": 1,
            "mission_count": 1, "last_used_at": 1,
        },
    ).sort("updated_at", -1)
    async for row in cur:
        out.append({
            "hostname": row.get("hostname"),
            "label": row.get("label") or row.get("hostname"),
            "updated_at": _iso(row.get("updated_at")),
            "last_used_at": _iso(row.get("last_used_at")),
            "mission_count": int(row.get("mission_count") or 0),
        })
    return out


async def delete_credentials(db, *, org_id: str, hostname: str) -> bool:
    host = host_to_etld_plus_one(hostname)
    if not host:
        return False
    res = await db.domain_credentials.delete_one(
        {"org_id": org_id, "hostname": host},
    )
    return bool(res.deleted_count)


async def merge_credentials_for_mission(
    db, *, org_id: str, urls: list[str],
) -> Optional[Dict[str, Any]]:
    """Build a combined storage_state covering every URL the mission goal
    references. We dedup by hostname (the login flow stores per-host
    state) and merge cookies + origins lists. Used by the mission start
    flow to pre-seed Chromium.
    """
    seen: set[str] = set()
    cookies: list[dict] = []
    origins: list[dict] = []
    for url in urls:
        host = url_to_etld_plus_one(url)
        if not host or host in seen:
            continue
        seen.add(host)
        state = await load_credentials(db=db, org_id=org_id, hostname=host)
        if not state:
            continue
        cookies.extend(state.get("cookies") or [])
        origins.extend(state.get("origins") or [])
        # Stamp last_used_at + bump mission_count for analytics.
        await db.domain_credentials.update_one(
            {"org_id": org_id, "hostname": host},
            {"$set": {"last_used_at": datetime.now(timezone.utc)},
             "$inc": {"mission_count": 1}},
        )
    if not cookies and not origins:
        return None
    return {"cookies": cookies, "origins": origins}


def _iso(d):
    return d.isoformat() if isinstance(d, datetime) else d


__all__ = [
    "host_to_etld_plus_one",
    "url_to_etld_plus_one",
    "looks_like_login_page",
    "save_credentials",
    "load_credentials",
    "list_credentials",
    "delete_credentials",
    "merge_credentials_for_mission",
]
