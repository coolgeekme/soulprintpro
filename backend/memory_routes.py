"""HTTP router for the user-scoped Memory store (Phase 1).

Endpoints (all gated by `get_current_user`):

    GET    /api/memory              → list the caller's own memories
    POST   /api/memory              → manually add a memory
    PATCH  /api/memory/{id}         → edit content / category / importance
                                       / is_archived  (only your own)
    DELETE /api/memory/{id}         → hard-delete (only your own)

The router is built via a factory so server.py can inject the live
Mongo handle + the auth dep without circular imports. Future Phase-2
scopes (team / org / shared) will extend the model in `memory.py`
and add a `scope=` query param here.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import memory as memory_mod


class MemoryCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=1000)
    category: str = "other"
    importance: str = "medium"


class MemoryUpdate(BaseModel):
    content: Optional[str] = Field(default=None, max_length=1000)
    category: Optional[str] = None
    importance: Optional[str] = None
    is_archived: Optional[bool] = None


def make_memory_router(*, db, get_current_user) -> APIRouter:
    r = APIRouter(prefix="/api/memory")

    @r.get("")
    async def list_my_memories(
        include_archived: bool = False,
        limit: int = 200,
        u: dict = Depends(get_current_user),
    ):
        rows = await memory_mod.list_memories(
            db, user_id=u["id"], scope="user",
            include_archived=include_archived, limit=min(limit, 500),
        )
        # Strip the dedupe key from API responses — it's an internal
        # implementation detail (and we already exclude `_id`).
        out = []
        for r_ in rows:
            r_.pop("content_key", None)
            out.append(r_)
        return {"memories": out, "categories": list(memory_mod.CATEGORIES)}

    @r.post("")
    async def add_my_memory(
        body: MemoryCreate, u: dict = Depends(get_current_user),
    ):
        row = await memory_mod.add_memory(
            db,
            user_id=u["id"],
            content=body.content,
            category=body.category,
            importance=body.importance,
            source="manual",
        )
        if not row:
            # add_memory swallows duplicate-key collisions silently — surface
            # a friendly conflict so the UI can react.
            raise HTTPException(409, "You already have a memory with this exact wording.")
        row.pop("content_key", None)
        return row

    @r.patch("/{memory_id}")
    async def edit_my_memory(
        memory_id: str, body: MemoryUpdate,
        u: dict = Depends(get_current_user),
    ):
        ok = await memory_mod.update_memory(
            db, memory_id=memory_id, user_id=u["id"],
            content=body.content, category=body.category,
            importance=body.importance, is_archived=body.is_archived,
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
