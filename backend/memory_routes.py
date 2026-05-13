"""HTTP router for the user-scoped Memory store (Phase 1).

Endpoints (all gated by `get_current_user`):

    GET    /api/memory?org_id=<id>         → list memories for this user,
                                              SCOPED to one org (strict).
    GET    /api/memory/untagged             → list user's untagged memories
                                              (legacy / migration UX).
    POST   /api/memory                      → manually add a memory.
                                              `org_id` is REQUIRED so we
                                              never create cross-org leaks.
    PATCH  /api/memory/{id}                 → edit content / category /
                                              importance / is_archived /
                                              org_id (used by the migration
                                              UI to assign a previously
                                              untagged memory to an org).
    DELETE /api/memory/{id}                 → hard-delete (own memories only).

STRICT ORG ISOLATION
====================
The chat-loop injection only reads memories whose `org_id` matches the
active org. Untagged memories (legacy rows from Phase 1 before this
change) are NEVER injected — the migration UI on /app/account shows
them in a dedicated "needs cleanup" section so the user can assign or
delete each one explicitly.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import memory as memory_mod
from bson import ObjectId


class MemoryCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=1000)
    org_id: str = Field(..., min_length=1, description="Which org this memory belongs to.")
    category: str = "other"
    importance: str = "medium"


class MemoryUpdate(BaseModel):
    content: Optional[str] = Field(default=None, max_length=1000)
    category: Optional[str] = None
    importance: Optional[str] = None
    is_archived: Optional[bool] = None
    org_id: Optional[str] = Field(default=None, description="Reassign to a different org.")


def _strip(row: dict) -> dict:
    row.pop("content_key", None)
    return row


def make_memory_router(*, db, get_current_user) -> APIRouter:
    r = APIRouter(prefix="/api/memory")

    async def _user_must_belong_to_org(user_id: str, org_id: str) -> None:
        """Defense-in-depth: require the caller to actually be a member
        of the org they're tagging a memory against. Prevents a malicious
        client from poisoning an unrelated org's memory pool."""
        try:
            ObjectId(org_id)
        except Exception:
            raise HTTPException(400, "Invalid org id.")
        membership = await db.org_members.find_one(
            {"org_id": org_id, "user_id": user_id},
            {"_id": 1},
        )
        if not membership:
            raise HTTPException(403, "You are not a member of that organization.")

    @r.get("")
    async def list_my_memories(
        org_id: Optional[str] = None,
        include_archived: bool = False,
        limit: int = 200,
        u: dict = Depends(get_current_user),
    ):
        """Scoped to ONE org by default. Without an `org_id` query param
        we still scope to "untagged" so the caller never sees other
        orgs' memories by accident.
        """
        if org_id:
            await _user_must_belong_to_org(u["id"], org_id)
            rows = await memory_mod.list_memories(
                db, user_id=u["id"], scope="user", org_id=org_id,
                include_archived=include_archived, limit=min(limit, 500),
            )
        else:
            # No org → return untagged only (for the migration UI).
            rows = await memory_mod.list_memories(
                db, user_id=u["id"], scope="user", org_id=None,
                include_archived=include_archived, limit=min(limit, 500),
            )
        return {
            "memories": [_strip(r_) for r_ in rows],
            "categories": list(memory_mod.CATEGORIES),
            "org_id": org_id,
        }

    @r.get("/untagged")
    async def list_my_untagged(u: dict = Depends(get_current_user)):
        """Convenience endpoint: every memory for this user where
        `org_id` is null. Drives the "needs cleanup" banner on /app/account."""
        rows = await memory_mod.list_memories(
            db, user_id=u["id"], scope="user", org_id=None,
            include_archived=True, limit=500,
        )
        return {
            "memories": [_strip(r_) for r_ in rows],
            "categories": list(memory_mod.CATEGORIES),
        }

    @r.post("")
    async def add_my_memory(
        body: MemoryCreate, u: dict = Depends(get_current_user),
    ):
        await _user_must_belong_to_org(u["id"], body.org_id)
        row = await memory_mod.add_memory(
            db,
            user_id=u["id"],
            content=body.content,
            category=body.category,
            importance=body.importance,
            source="manual",
            org_id=body.org_id,
        )
        if not row:
            raise HTTPException(
                409,
                "You already have a memory with this exact wording in this org.",
            )
        return _strip(row)

    @r.patch("/{memory_id}")
    async def edit_my_memory(
        memory_id: str, body: MemoryUpdate,
        u: dict = Depends(get_current_user),
    ):
        # If reassigning, verify membership of the new org before letting
        # the update through. Skip the membership check when org_id is
        # not being changed (None means "don't touch").
        update_kwargs = {
            "content": body.content,
            "category": body.category,
            "importance": body.importance,
            "is_archived": body.is_archived,
        }
        if body.org_id is not None:
            await _user_must_belong_to_org(u["id"], body.org_id)
            update_kwargs["org_id"] = body.org_id
        ok = await memory_mod.update_memory(
            db, memory_id=memory_id, user_id=u["id"],
            **update_kwargs,
        )
        if not ok:
            raise HTTPException(404, "Memory not found or nothing to update.")
        return {"ok": True}

    @r.delete("/{memory_id}")
    async def delete_my_memory(
        memory_id: str, u: dict = Depends(get_current_user),
    ):
        ok = await memory_mod.delete_memory(
            db, memory_id=memory_id, user_id=u["id"],
        )
        if not ok:
            raise HTTPException(404, "Memory not found.")
        return {"ok": True}

    return r
