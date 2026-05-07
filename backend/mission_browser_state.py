"""Per-mission Chromium session state persistence.

Solves "live browser is blank after a deploy" by continuously snapshotting
the mission's Chromium `storage_state` (cookies + localStorage + IndexedDB
metadata) to MongoDB during a run. When the backend restarts, we rebuild
the sticky page from disk so the user resumes EXACTLY where they were —
authenticated state intact.

Key differences from `credential_vault`:
  • Vault is keyed by (org_id, hostname) and captures EXPLICIT user
    "save login" actions for cross-mission reuse.
  • This module is keyed by `mission_id` and captures the FULL page state
    (every cookie the agent encountered, plus the last URL) for survival
    of in-flight missions across restarts.

Schema (`mission_browser_states` collection):
  {
    _id, mission_id,                          # primary key
    org_id,                                   # for cleanup/audit
    storage_state_encrypted: bytes,           # Fernet ciphertext
    last_url: str,                            # navigate back here on restore
    bytes_size: int,                          # informational
    updated_at, expires_at,                   # TTL purge
    capture_count: int,
  }

Encryption uses the same `_fernet()` helper as `credential_vault` so
SOULPRINT_VAULT_KEY rotation behaves consistently.

TTL: rows expire 7 days after last update. Missions that finish/cancel
have their row deleted explicitly via `purge_mission_state(mission_id)`
on terminal-state transitions.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from cryptography.fernet import InvalidToken

from credential_vault import _fernet  # reuse the same key + Fernet instance

logger = logging.getLogger(__name__)

# Cap a single state payload to keep us from accidentally bloating Mongo
# with megabytes of cookie blobs from rogue sites. Real-world states for
# Twitter / LinkedIn / Instagram / Stripe are all <100 KB.
_MAX_STATE_BYTES = 512 * 1024  # 512 KB cipher cap

# How long before a row is auto-purged. Missions rarely run >7d, and we
# want the live-browser revival to work a long weekend after a deploy.
_TTL = timedelta(days=7)


async def ensure_indexes(db) -> None:
    """Idempotent index creation. Called once on app startup."""
    try:
        await db.mission_browser_states.create_index("mission_id", unique=True)
        # Mongo TTL index — purges any state where expires_at is in the past.
        await db.mission_browser_states.create_index(
            "expires_at", expireAfterSeconds=0,
        )
    except Exception as exc:
        logger.warning("mission_browser_state index creation skipped: %s", exc)


async def save_mission_state(
    db, *, mission_id: str, org_id: str, storage_state: Dict[str, Any],
    last_url: str = "",
) -> bool:
    """Encrypt and upsert the mission's current Chromium state.

    Returns True on success. Silently no-ops + logs if the payload exceeds
    the size cap (so a bloated state can't break the agent's main flow).
    """
    if not mission_id:
        return False
    try:
        payload = json.dumps(storage_state, default=str).encode("utf-8")
    except Exception as exc:
        logger.debug("Mission %s: state serialise failed: %s", mission_id, exc)
        return False
    cipher = _fernet().encrypt(payload)
    if len(cipher) > _MAX_STATE_BYTES:
        logger.warning(
            "Mission %s: state too large (%d bytes), skipping persist",
            mission_id, len(cipher),
        )
        return False
    now = datetime.now(timezone.utc)
    try:
        await db.mission_browser_states.update_one(
            {"mission_id": mission_id},
            {
                "$set": {
                    "org_id": org_id,
                    "storage_state_encrypted": cipher,
                    "last_url": (last_url or "").strip()[:2048],
                    "bytes_size": len(cipher),
                    "updated_at": now,
                    "expires_at": now + _TTL,
                },
                "$setOnInsert": {
                    "mission_id": mission_id,
                    "created_at": now,
                },
                "$inc": {"capture_count": 1},
            },
            upsert=True,
        )
        return True
    except Exception as exc:
        logger.warning("Mission %s: state save failed: %s", mission_id, exc)
        return False


async def load_mission_state(
    db, *, mission_id: str,
) -> Optional[Dict[str, Any]]:
    """Fetch + decrypt a mission's persisted state, returning a dict like
    `{storage_state, last_url}` or None on miss / decrypt failure.

    On undecryptable rows (key rotation), the row is deleted so the next
    capture starts fresh — same behaviour as `credential_vault`.
    """
    if not mission_id:
        return None
    row = await db.mission_browser_states.find_one(
        {"mission_id": mission_id}, {"_id": 1, "storage_state_encrypted": 1, "last_url": 1},
    )
    if not row:
        return None
    cipher = row.get("storage_state_encrypted")
    if not cipher:
        return None
    try:
        plain = _fernet().decrypt(cipher)
        state = json.loads(plain.decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "Mission %s: state decrypt failed (%s) — purging row",
            mission_id, exc,
        )
        await db.mission_browser_states.delete_one({"_id": row["_id"]})
        return None
    return {
        "storage_state": state,
        "last_url": row.get("last_url") or "",
    }


async def purge_mission_state(db, *, mission_id: str) -> bool:
    """Delete a mission's state row. Called when the mission reaches a
    terminal state (finished/cancelled/failed) so we don't keep cookies
    around longer than needed."""
    if not mission_id:
        return False
    try:
        res = await db.mission_browser_states.delete_one({"mission_id": mission_id})
        return bool(res.deleted_count)
    except Exception as exc:
        logger.debug("Mission %s: state purge failed: %s", mission_id, exc)
        return False


__all__ = [
    "ensure_indexes",
    "save_mission_state",
    "load_mission_state",
    "purge_mission_state",
]
