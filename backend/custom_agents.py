"""custom_agents.py — per-org user-defined agents.

Custom agents live alongside the 169 baked-in agents in the in-process
AGENTS dict (registered at startup + on create/update/delete) so every
existing AGENTS[aid] lookup, smart-router, mention parser, and prompt
builder keeps working with zero patching.

The only differentiator: a custom agent carries an `org_scope` field
naming the org that owns it. `team_agents` filters the catalogue so a
custom agent only appears in teams that belong to its own org.

Schema (collection: custom_agents):
  { id, org_id, name, role, description, system,
    division, division_key, emoji, accent,
    created_by, created_at, updated_at }
"""

from datetime import datetime, timezone
from typing import Optional
import uuid

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorDatabase


# ── Wired in by server.py at startup ────────────────────────────────────────
_db: Optional[AsyncIOMotorDatabase] = None
_agents_dict: Optional[dict] = None
_get_current_user = None
_require_org_member = None


def init(*, db, agents_dict, get_current_user, require_org_member):
    """Call once from server.py after definitions exist."""
    global _db, _agents_dict, _get_current_user, _require_org_member
    _db = db
    _agents_dict = agents_dict
    _get_current_user = get_current_user
    _require_org_member = require_org_member


async def _user_dep(request: Request) -> dict:
    """FastAPI dependency that delegates to the host app's auth helper.

    We use a thin wrapper so the underlying `get_current_user` (defined
    in server.py) can be hot-swapped in tests without re-mounting the
    router.
    """
    if _get_current_user is None:
        raise HTTPException(500, "custom_agents not initialised")
    return await _get_current_user(request)


def _public(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "_id"}


def _to_catalogue_entry(doc: dict) -> dict:
    """Shape a custom-agent Mongo row into the same shape AGENTS uses."""
    return {
        "id": doc["id"],
        "name": doc["name"],
        "role": doc.get("role") or doc["name"],
        "division": doc.get("division", "Custom"),
        "division_key": doc.get("division_key", "custom"),
        "description": doc.get("description", ""),
        "tagline": doc.get("description", ""),
        "emoji": doc.get("emoji", ""),
        "accent": doc.get("accent", "#f97316"),
        "system": doc.get("system", ""),
        "custom": True,
        "org_scope": doc["org_id"],
    }


async def load_into_catalogue():
    """Read every persisted custom agent and merge into AGENTS at boot."""
    if _db is None or _agents_dict is None:
        return 0
    n = 0
    async for doc in _db.custom_agents.find({}):
        _agents_dict[doc["id"]] = _to_catalogue_entry(doc)
        n += 1
    return n


async def _require_admin(org_id: str, user_id: str):
    """Owner/admin gate — used for create/update/delete."""
    ctx = await _require_org_member(org_id, user_id)
    role = (ctx.get("member") or {}).get("role", "")
    if role not in ("owner", "admin"):
        raise HTTPException(
            403, "Only the organization owner or admins can manage custom agents."
        )
    return ctx


# ── Pydantic bodies ─────────────────────────────────────────────────────────
class CustomAgentBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    role: Optional[str] = Field(None, max_length=120)
    description: Optional[str] = Field(None, max_length=400)
    system: str = Field(..., min_length=10, max_length=8000)
    emoji: Optional[str] = Field(None, max_length=8)
    accent: Optional[str] = Field(None, max_length=20)


# ── Router ──────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/orgs")


@router.get("/{org_id}/custom-agents")
async def list_custom_agents(org_id: str, user: dict = Depends(_user_dep)):
    await _require_org_member(org_id, user["id"])
    out = []
    async for doc in _db.custom_agents.find({"org_id": org_id}).sort("created_at", -1):
        out.append(_public(doc))
    return out


@router.post("/{org_id}/custom-agents")
async def create_custom_agent(
    org_id: str, body: CustomAgentBody, user: dict = Depends(_user_dep)
):
    await _require_admin(org_id, user["id"])

    aid = f"custom_{org_id[:8]}_{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": aid,
        "org_id": org_id,
        "name": body.name.strip(),
        "role": (body.role or body.name).strip(),
        "description": (body.description or "").strip(),
        "system": body.system.strip(),
        "emoji": (body.emoji or "").strip()[:8],
        "accent": (body.accent or "#f97316").strip()[:20],
        "division": "Custom",
        "division_key": "custom",
        "created_by": user["id"],
        "created_at": now,
        "updated_at": now,
    }
    await _db.custom_agents.insert_one(doc.copy())
    _agents_dict[aid] = _to_catalogue_entry(doc)
    return _public(doc)


@router.patch("/{org_id}/custom-agents/{agent_id}")
async def update_custom_agent(
    org_id: str,
    agent_id: str,
    body: CustomAgentBody,
    user: dict = Depends(_user_dep),
):
    await _require_admin(org_id, user["id"])

    existing = await _db.custom_agents.find_one({"id": agent_id, "org_id": org_id})
    if not existing:
        raise HTTPException(404, "Custom agent not found")

    updates = {
        "name": body.name.strip(),
        "role": (body.role or body.name).strip(),
        "description": (body.description or "").strip(),
        "system": body.system.strip(),
        "emoji": (body.emoji or "").strip()[:8],
        "accent": (body.accent or "#f97316").strip()[:20],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await _db.custom_agents.update_one(
        {"id": agent_id, "org_id": org_id}, {"$set": updates}
    )
    refreshed = await _db.custom_agents.find_one({"id": agent_id, "org_id": org_id})
    _agents_dict[agent_id] = _to_catalogue_entry(refreshed)
    return _public(refreshed)


@router.delete("/{org_id}/custom-agents/{agent_id}")
async def delete_custom_agent(
    org_id: str, agent_id: str, user: dict = Depends(_user_dep)
):
    await _require_admin(org_id, user["id"])
    res = await _db.custom_agents.delete_one({"id": agent_id, "org_id": org_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Custom agent not found")
    _agents_dict.pop(agent_id, None)
    return {"ok": True}
