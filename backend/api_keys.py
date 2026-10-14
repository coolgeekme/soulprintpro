"""Revocable per-user API keys — the long-lived credential for MCP/agent access.

A Foundry JWT session token is short-lived and cookie-bound, which is the wrong
credential for an MCP client that stores it in a config file (the connection
breaks on every expiry and the operator re-pastes a token forever). API keys are:

  - long-lived   — no expiry; revoke to kill
  - hashed       — sha256 at rest; plaintext shown exactly once, at creation
  - user-scoped  — resolve to the owner's user, so the key inherits that user's
                   normal permissions, tenancy, and audit trail (nothing widened)
  - revocable    — `revoked_at` stamp, audited via `last_used_at`

Key format: ``fnd_`` + 32 hex chars (16 random bytes) so ``get_current_user`` can
distinguish an API key from a JWT / Firebase token in one cheap prefix check.

`allowed_tools` is stored for future per-key scoping (an empty list means
UNRESTRICTED, never deny-all). Enforcement lives at the MCP boundary, not here —
the REST API has no concept of MCP tool names.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

API_KEY_PREFIX = "fnd_"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_api_key() -> str:
    """16 random bytes → 32 hex chars, prefixed so it is cheaply recognisable."""
    return API_KEY_PREFIX + secrets.token_hex(16)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


async def resolve_api_key(db, key: str) -> Optional[dict]:
    """Return the owning user's full doc for a valid, un-revoked key, else None.

    Fails closed: missing, revoked, and orphaned (owner deleted) keys all return
    None, and the caller raises 401. Updates `last_used_at` fire-and-forget.
    """
    h = hash_api_key(key)
    rec = await db.api_keys.find_one({"key_hash": h})
    if not rec or rec.get("revoked_at"):
        return None
    user = await db.users.find_one({"_id": ObjectId(rec["user_id"])})
    if not user:
        return None
    try:
        await db.api_keys.update_one(
            {"_id": rec["_id"]}, {"$set": {"last_used_at": _now()}},
        )
    except Exception:
        pass  # telemetry must never block auth
    return user


class CreateKeyBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    # Empty list = unrestricted (mirror the key's owner's permissions).
    allowed_tools: list[str] = Field(default_factory=list)


def make_api_keys_router(*, db, get_current_user) -> APIRouter:
    r = APIRouter(prefix="/api/keys")

    @r.post("")
    async def create_key(body: CreateKeyBody, u: dict = Depends(get_current_user)):
        key = new_api_key()
        # Idempotent unique index — a hash collision is astronomically unlikely,
        # but the index guarantees it can't silently overwrite another user's key.
        try:
            await db.api_keys.create_index("key_hash", unique=True)
        except Exception:
            pass
        await db.api_keys.insert_one({
            "key_hash": hash_api_key(key),
            "user_id": u["id"],
            "name": body.name.strip(),
            "allowed_tools": body.allowed_tools,
            "created_at": _now(),
            "last_used_at": None,
            "revoked_at": None,
        })
        # The raw key is returned EXACTLY ONCE. It is not stored anywhere.
        return {
            "key": key,
            "name": body.name.strip(),
            "allowed_tools": body.allowed_tools,
        }

    @r.get("")
    async def list_keys(u: dict = Depends(get_current_user)):
        """Metadata only — never the raw key, never the hash."""
        out = []
        cur = db.api_keys.find({"user_id": u["id"], "revoked_at": None})
        async for k in cur:
            out.append({
                "id": str(k["_id"]),
                "name": k.get("name"),
                "allowed_tools": k.get("allowed_tools") or [],
                "created_at": k.get("created_at"),
                "last_used_at": k.get("last_used_at"),
            })
        return out

    @r.delete("/{key_id}")
    async def revoke_key(key_id: str, u: dict = Depends(get_current_user)):
        res = await db.api_keys.update_one(
            {"_id": ObjectId(key_id), "user_id": u["id"]},
            {"$set": {"revoked_at": _now()}},
        )
        if not res.matched_count:
            raise HTTPException(404, "Key not found")
        return {"revoked": True}

    return r


__all__ = [
    "API_KEY_PREFIX",
    "new_api_key",
    "hash_api_key",
    "resolve_api_key",
    "make_api_keys_router",
]
