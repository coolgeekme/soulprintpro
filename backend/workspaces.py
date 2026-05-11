"""Workspace layer — the billable entity above Organizations.

Hierarchy (introduced May 2026):
    Workspace        ← NEW; top-level, eventually billable
      ├ Members      (role: owner | admin | member) — stored in `workspace_members`
      └ Organizations (each Org has workspace_id) — existing `organizations`
         ├ Members   (role: admin | member) — existing `org_members`
         └ Teams     — existing `teams`

Why this shape:
  • Single owner / agency → many client Orgs → many teams per Org fits
    the product vision without forcing every customer into a huge refactor.
  • Billing can roll up to the workspace: plan tier, included org count,
    included seats, overages charged per extra Org / seat.
  • Auto-migration on startup: every owner gets one personal workspace
    named "{Owner Name} Workspace" and every Org they own moves under it.
    Zero data loss, all existing endpoints keep working.

Role model:
  • Workspace owner  → single user; inherits admin of every Org in the
    workspace (no extra row required — `require_org_member` special-cases
    the owner-of-parent-workspace).
  • Workspace admin  → can invite to workspace, create new Orgs, edit
    plan. Does NOT automatically gain Org-level access; per-Org grants
    still needed.
  • Workspace member → base level; Org access is granted explicitly on
    the invite or later via the members UI.

Invite flow (W3):
  • Owner/admin creates an invite: `{email, ws_role, org_grants: [{org_id, role}]}`.
  • Invitee accepts → we upsert the workspace membership AND create each
    `org_members` row in one transaction-ish sequence.

Removal:
  • Removing a user from the workspace revokes every Org membership in
    that workspace (clean break per product spec).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from email_service import send_resend_email

logger = logging.getLogger(__name__)


# ── Pydantic bodies ────────────────────────────────────────────────────

class WorkspaceCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class WorkspaceUpdateBody(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)


class OrgGrant(BaseModel):
    """One entry in the invite's `org_grants` list — tells the acceptor
    which Org(s) they should land in, and with what role."""
    org_id: str
    role: str = "member"  # "admin" | "member"


class WorkspaceInviteBody(BaseModel):
    email: Optional[EmailStr] = None   # optional: code is always returned
    ws_role: str = "member"            # "admin" | "member"
    org_grants: list[OrgGrant] = []


class MemberOrgAccessPatch(BaseModel):
    """Replace (not merge) an existing workspace member's Org access.
    Sending `[]` revokes them from every Org in the workspace."""
    org_grants: list[OrgGrant] = []


# ── Helpers ──────────────────────────────────────────────────────────


def _public_origin(request: Request) -> str:
    """Compute the correct public-facing origin (scheme://host) for
    building share links in emails. We can't trust a hardcoded
    `FRONTEND_URL` env var because the same backend image runs in
    BOTH preview and production — a single env var would always be
    wrong in one of them.

    Priority:
      1. ``X-Forwarded-Host`` + ``X-Forwarded-Proto`` (set by the
         platform's ingress/Cloudflare → uvicorn). When the request
         arrives at `foundryagents.ai`, these headers reflect
         that origin and the resulting invite URL points at the right
         deployment.
      2. ``Origin`` request header — set by the browser's fetch from
         the SPA. Useful when the platform layer doesn't forward host.
      3. ``Host`` header (always present).
      4. Final fallback: ``FRONTEND_URL`` env var (last-ditch).

    Returns a string like ``"https://foundryagents.ai"`` with no
    trailing slash. Always validates the scheme is http/https to avoid
    header-injection attacks producing ``javascript:`` links.

    Operator escape hatch: setting ``PUBLIC_ORIGIN_OVERRIDE`` in env
    pins the result regardless of headers — needed when a fronting
    proxy (e.g. emergent.host → cloudflare → foundryagents.ai)
    rewrites X-Forwarded-Host to the inner deploy URL so emails would
    otherwise leak the wrong domain.
    """
    override = (os.environ.get("PUBLIC_ORIGIN_OVERRIDE") or "").strip().rstrip("/")
    if override.startswith("http://") or override.startswith("https://"):
        return override
    fwd_host = (request.headers.get("x-forwarded-host") or "").strip()
    if fwd_host:
        # X-Forwarded-Host can be a comma-separated chain — first entry
        # is the original client-facing hostname.
        fwd_host = fwd_host.split(",")[0].strip()
    fwd_proto = (request.headers.get("x-forwarded-proto") or "").strip().split(",")[0].strip()
    if fwd_host:
        scheme = "https" if fwd_proto in {"https", ""} else fwd_proto
        if scheme in {"http", "https"}:
            return f"{scheme}://{fwd_host}"
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if origin.startswith("http://") or origin.startswith("https://"):
        return origin
    host = (request.headers.get("host") or "").strip()
    if host:
        return f"https://{host}"
    return (os.environ.get("FRONTEND_URL") or "").rstrip("/")


async def _load_workspace(db, ws_id: str) -> dict:
    try:
        ws = await db.workspaces.find_one({"_id": ObjectId(ws_id)})
    except Exception:
        raise HTTPException(400, "Invalid workspace id")
    if not ws:
        raise HTTPException(404, "Workspace not found")
    return ws


async def require_workspace_member(
    db, ws_id: str, user_id: str, *, required_role: Optional[str] = None,
) -> dict:
    """Fail with 403 unless the user is a member of the workspace.

    When `required_role` is set (`"admin"` or `"owner"`) the user's role
    must be AT LEAST that (`owner` > `admin` > `member`).
    """
    ws = await _load_workspace(db, ws_id)
    m = await db.workspace_members.find_one(
        {"workspace_id": ws_id, "user_id": user_id},
    )
    if not m:
        raise HTTPException(403, "Not a member of this workspace")
    if required_role:
        order = {"owner": 3, "admin": 2, "member": 1}
        if order.get(m.get("role"), 0) < order.get(required_role, 0):
            raise HTTPException(403, f"Requires workspace {required_role}")
    return {"workspace": ws, "member": m}


async def _serialize_workspace(
    db, ws: dict, *, user_id: str = "",
) -> dict:
    ws_id = str(ws["_id"])
    # Count Orgs + members via quick count_documents — cheap with indexes.
    org_count = await db.organizations.count_documents({"workspace_id": ws_id})
    member_count = await db.workspace_members.count_documents({"workspace_id": ws_id})
    my_role = ""
    if user_id:
        m = await db.workspace_members.find_one(
            {"workspace_id": ws_id, "user_id": user_id}, {"role": 1},
        )
        my_role = (m or {}).get("role", "")
    return {
        "id": ws_id,
        "name": ws.get("name", ""),
        "owner_id": ws.get("owner_id", ""),
        "created_at": _iso(ws.get("created_at")),
        "plan": ws.get("plan", "free"),          # future billing tier
        "org_count": int(org_count),
        "member_count": int(member_count),
        "my_role": my_role,
    }


def _iso(d):
    return d.isoformat() if isinstance(d, datetime) else d


# ── One-shot migration: run on every backend startup ──────────────────

async def migrate_existing_orgs_to_workspaces(db) -> int:
    """Ensure every Org has a `workspace_id`. Groups Orgs by their
    current owner into a single personal workspace per owner named
    "{display name}'s Workspace". Idempotent — running a second time is
    a no-op. Returns the number of Orgs migrated.

    Also backfills `workspace_members` for every Org owner so the
    workspace membership is consistent on day one.
    """
    migrated = 0
    cur = db.organizations.find(
        {"workspace_id": {"$in": [None, ""]}},
        {"_id": 1, "name": 1, "owner_id": 1, "created_at": 1},
    )
    owner_cache: dict[str, str] = {}
    async for org in cur:
        owner_id = org.get("owner_id") or ""
        if not owner_id:
            # Orphan org — skip; will be cleaned up manually.
            continue
        ws_id = owner_cache.get(owner_id)
        if not ws_id:
            # Look for an existing personal workspace for this owner.
            existing = await db.workspaces.find_one(
                {"owner_id": owner_id, "kind": "personal"},
            )
            if existing:
                ws_id = str(existing["_id"])
            else:
                # Fetch the owner's display name so the workspace label
                # isn't just an opaque uuid.
                user = await db.users.find_one(
                    {"_id": ObjectId(owner_id)}, {"name": 1, "email": 1},
                )
                label = (
                    (user or {}).get("name")
                    or ((user or {}).get("email") or "").split("@", 1)[0]
                    or "My"
                )
                now = datetime.now(timezone.utc)
                ws_doc = {
                    "name": f"{label}'s Workspace",
                    "owner_id": owner_id,
                    "kind": "personal",
                    "plan": "free",
                    "created_at": now,
                }
                res = await db.workspaces.insert_one(ws_doc)
                ws_id = str(res.inserted_id)
                # Owner row on workspace_members.
                await db.workspace_members.update_one(
                    {"workspace_id": ws_id, "user_id": owner_id},
                    {"$setOnInsert": {
                        "workspace_id": ws_id,
                        "user_id": owner_id,
                        "role": "owner",
                        "joined_at": now,
                    }},
                    upsert=True,
                )
                logger.info("Workspace: created personal workspace %s for owner %s",
                            ws_id, owner_id)
            owner_cache[owner_id] = ws_id
        # Stamp the Org with its new workspace_id.
        await db.organizations.update_one(
            {"_id": org["_id"]},
            {"$set": {"workspace_id": ws_id}},
        )
        migrated += 1
    if migrated:
        logger.info("Workspace migration: stamped workspace_id on %d org(s)", migrated)
    return migrated


# ── Router factory ───────────────────────────────────────────────────

def make_workspaces_router(
    *, db, get_current_user, require_org_member, get_user_name: Any = None,
) -> APIRouter:
    r = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

    # ── List / get / create / update workspaces ─────────────────────
    @r.get("")
    async def list_my_workspaces(u: dict = Depends(get_current_user)):
        memberships = await db.workspace_members.find(
            {"user_id": u["id"]}, {"workspace_id": 1, "role": 1},
        ).to_list(200)
        ids = [m["workspace_id"] for m in memberships]
        if not ids:
            return []
        object_ids = []
        for _id in ids:
            try:
                object_ids.append(ObjectId(_id))
            except Exception:
                pass
        ws_docs = await db.workspaces.find({"_id": {"$in": object_ids}}).to_list(200)
        out = [await _serialize_workspace(db, w, user_id=u["id"]) for w in ws_docs]
        return out

    @r.get("/{ws_id}")
    async def get_workspace(ws_id: str, u: dict = Depends(get_current_user)):
        ctx = await require_workspace_member(db, ws_id, u["id"])
        return await _serialize_workspace(db, ctx["workspace"], user_id=u["id"])

    @r.post("")
    async def create_workspace(
        body: WorkspaceCreateBody, u: dict = Depends(get_current_user),
    ):
        now = datetime.now(timezone.utc)
        ws_doc = {
            "name": body.name.strip(),
            "owner_id": u["id"],
            "kind": "personal",
            "plan": "free",
            "created_at": now,
        }
        res = await db.workspaces.insert_one(ws_doc)
        ws_id = str(res.inserted_id)
        await db.workspace_members.insert_one({
            "workspace_id": ws_id,
            "user_id": u["id"],
            "role": "owner",
            "joined_at": now,
        })
        ws_doc["_id"] = res.inserted_id
        return await _serialize_workspace(db, ws_doc, user_id=u["id"])

    @r.patch("/{ws_id}")
    async def update_workspace(
        ws_id: str, body: WorkspaceUpdateBody,
        u: dict = Depends(get_current_user),
    ):
        await require_workspace_member(db, ws_id, u["id"], required_role="admin")
        updates: dict[str, Any] = {}
        if body.name is not None:
            updates["name"] = body.name.strip()
        if updates:
            await db.workspaces.update_one(
                {"_id": ObjectId(ws_id)}, {"$set": updates},
            )
        ws = await _load_workspace(db, ws_id)
        return await _serialize_workspace(db, ws, user_id=u["id"])

    # ── Org listing (scoped to workspace) ────────────────────────────
    @r.get("/{ws_id}/orgs")
    async def list_workspace_orgs(
        ws_id: str, u: dict = Depends(get_current_user),
    ):
        await require_workspace_member(db, ws_id, u["id"])
        cur = db.organizations.find(
            {"workspace_id": ws_id},
            {
                "_id": 1, "name": 1, "website_url": 1,
                "owner_id": 1, "created_at": 1,
            },
        ).sort("created_at", -1)
        out = []
        async for o in cur:
            # Mark whether the caller has explicit Org-level access.
            has_org_access = await db.org_members.find_one(
                {"org_id": str(o["_id"]), "user_id": u["id"]}, {"_id": 1},
            )
            out.append({
                "id": str(o["_id"]),
                "name": o.get("name"),
                "website_url": o.get("website_url", ""),
                "created_at": _iso(o.get("created_at")),
                "my_org_role": (
                    (await db.org_members.find_one(
                        {"org_id": str(o["_id"]), "user_id": u["id"]},
                        {"role": 1},
                    ) or {}).get("role", "")
                    if has_org_access else ""
                ),
            })
        return out

    # ── Members ─────────────────────────────────────────────────────
    @r.get("/{ws_id}/members")
    async def list_members(
        ws_id: str, u: dict = Depends(get_current_user),
    ):
        await require_workspace_member(db, ws_id, u["id"])
        cur = db.workspace_members.find({"workspace_id": ws_id})
        rows = []
        async for m in cur:
            uid = m.get("user_id")
            user = None
            if uid:
                try:
                    user = await db.users.find_one(
                        {"_id": ObjectId(uid)},
                        {"name": 1, "email": 1, "avatar_url": 1},
                    )
                except Exception:
                    user = None
            # Collect the list of Orgs (in this workspace) that this
            # user has access to — useful for the members grid.
            org_access_cur = db.org_members.find(
                {"user_id": uid}, {"org_id": 1, "role": 1},
            )
            org_ids_in_ws_cur = db.organizations.find(
                {"workspace_id": ws_id}, {"_id": 1},
            )
            org_ids_in_ws = {
                str(o["_id"]) async for o in org_ids_in_ws_cur
            }
            org_access = []
            async for om in org_access_cur:
                if om.get("org_id") in org_ids_in_ws:
                    org_access.append({
                        "org_id": om.get("org_id"),
                        "role": om.get("role"),
                    })
            rows.append({
                "user_id": uid,
                "role": m.get("role", "member"),
                "joined_at": _iso(m.get("joined_at")),
                "name": (user or {}).get("name", ""),
                "email": (user or {}).get("email", ""),
                "avatar_url": (user or {}).get("avatar_url", ""),
                "org_access": org_access,
            })
        return rows

    @r.patch("/{ws_id}/members/{user_id}/org-access")
    async def update_member_org_access(
        ws_id: str, user_id: str, body: MemberOrgAccessPatch,
        u: dict = Depends(get_current_user),
    ):
        await require_workspace_member(db, ws_id, u["id"], required_role="admin")
        # Verify target user is a workspace member.
        target = await db.workspace_members.find_one(
            {"workspace_id": ws_id, "user_id": user_id},
        )
        if not target:
            raise HTTPException(404, "Target user is not a workspace member")
        # Build the set of current Org memberships in this workspace.
        org_ids_in_ws = {
            str(o["_id"]) async for o in db.organizations.find(
                {"workspace_id": ws_id}, {"_id": 1},
            )
        }
        wanted = {
            g.org_id: g.role for g in body.org_grants
            if g.org_id in org_ids_in_ws
        }
        # Current Org rows for this user that live in this workspace.
        current_rows = [
            om async for om in db.org_members.find(
                {"user_id": user_id, "org_id": {"$in": list(org_ids_in_ws)}},
            )
        ]
        current_map = {r["org_id"]: r for r in current_rows}

        now = datetime.now(timezone.utc)
        # Add + upgrade / downgrade.
        for org_id, role in wanted.items():
            if org_id in current_map:
                # Role change only if different (owners are left alone).
                if current_map[org_id].get("role") not in {"owner"} and \
                        current_map[org_id].get("role") != role:
                    await db.org_members.update_one(
                        {"_id": current_map[org_id]["_id"]},
                        {"$set": {"role": role}},
                    )
            else:
                await db.org_members.insert_one({
                    "org_id": org_id, "user_id": user_id,
                    "role": role, "joined_at": now,
                })
        # Revoke Orgs that were in current_map but not in wanted —
        # never touch the `owner` row (the workspace owner is always an
        # Org admin implicitly; we don't revoke ownership here).
        for org_id, row in current_map.items():
            if org_id not in wanted and row.get("role") != "owner":
                await db.org_members.delete_one({"_id": row["_id"]})
        return {"ok": True}

    @r.delete("/{ws_id}/members/{user_id}")
    async def remove_member(
        ws_id: str, user_id: str,
        u: dict = Depends(get_current_user),
    ):
        ctx = await require_workspace_member(
            db, ws_id, u["id"], required_role="admin",
        )
        if ctx["workspace"].get("owner_id") == user_id:
            raise HTTPException(400, "Cannot remove the workspace owner")
        # Clean break: drop every Org membership within this workspace.
        org_ids_in_ws = [
            str(o["_id"]) async for o in db.organizations.find(
                {"workspace_id": ws_id}, {"_id": 1},
            )
        ]
        if org_ids_in_ws:
            await db.org_members.delete_many(
                {"user_id": user_id, "org_id": {"$in": org_ids_in_ws},
                 "role": {"$ne": "owner"}},
            )
        await db.workspace_members.delete_one(
            {"workspace_id": ws_id, "user_id": user_id},
        )
        return {"ok": True}

    # ── Invites ─────────────────────────────────────────────────────
    @r.post("/{ws_id}/invites")
    async def create_ws_invite(
        ws_id: str, body: WorkspaceInviteBody,
        request: Request,
        u: dict = Depends(get_current_user),
    ):
        await require_workspace_member(db, ws_id, u["id"], required_role="admin")
        # Validate every org_grant is actually inside this workspace —
        # prevents a rogue admin from granting access to another
        # workspace's Org via the invite code.
        if body.org_grants:
            org_ids = [g.org_id for g in body.org_grants]
            valid_ids = {
                str(o["_id"]) async for o in db.organizations.find(
                    {"workspace_id": ws_id,
                     "_id": {"$in": [ObjectId(i) for i in org_ids if len(i) == 24]}},
                    {"_id": 1},
                )
            }
            for g in body.org_grants:
                if g.org_id not in valid_ids:
                    raise HTTPException(
                        400,
                        f"Org {g.org_id} is not part of this workspace",
                    )
                if g.role not in {"admin", "member"}:
                    raise HTTPException(400, "Org role must be admin or member")
        if body.ws_role not in {"admin", "member"}:
            raise HTTPException(400, "Workspace role must be admin or member")
        code = secrets.token_urlsafe(10)
        now = datetime.now(timezone.utc)
        doc = {
            "workspace_id": ws_id,
            "email": (body.email or "").lower().strip() or None,
            "ws_role": body.ws_role,
            "org_grants": [g.model_dump() for g in body.org_grants],
            "code": code,
            "created_by": u["id"],
            "created_at": now,
            "expires_at": now + timedelta(days=14),
            "used_by": None,
        }
        await db.workspace_invites.insert_one(doc)
        # Fire-and-forget email notification. We don't block the
        # response on delivery — if Resend is unreachable the invite
        # row is already persisted and the inviter can still "Copy link".
        if body.email:
            try:
                frontend = _public_origin(request)
                invite_url = f"{frontend}/invite/{code}"
                ws = await db.workspaces.find_one(
                    {"_id": ObjectId(ws_id)}, {"name": 1},
                )
                ws_name = (ws or {}).get("name") or "a workspace"
                # Enrich org-grants with display names for the email body.
                org_lines_text: list[str] = []
                org_lines_html: list[str] = []
                if body.org_grants:
                    org_ids = [
                        ObjectId(g.org_id) for g in body.org_grants
                        if len(g.org_id) == 24
                    ]
                    org_names: dict[str, str] = {}
                    if org_ids:
                        async for o in db.organizations.find(
                            {"_id": {"$in": org_ids}}, {"name": 1},
                        ):
                            org_names[str(o["_id"])] = o.get("name", "")
                    for g in body.org_grants:
                        n = org_names.get(g.org_id) or "an organization"
                        org_lines_text.append(f"  • {n} — {g.role}")
                        org_lines_html.append(
                            f"<li style='margin:4px 0'>"
                            f"<b>{n}</b> "
                            f"<span style='color:#888;font-size:12px;"
                            f"text-transform:uppercase;letter-spacing:0.05em'>"
                            f"{g.role}</span></li>"
                        )
                inviter_name = u.get("name") or (u.get("email") or "").split("@", 1)[0]
                org_list_text = "\n".join(org_lines_text) if org_lines_text else \
                    "  (no org access yet — the owner will grant it after you join)"
                org_list_html = (
                    f"<ul style='padding-left:18px;margin:8px 0'>{''.join(org_lines_html)}</ul>"
                    if org_lines_html
                    else "<p style='color:#666;font-size:13px'>No org access yet "
                         "— the owner will grant it after you join.</p>"
                )
                subject = f"{inviter_name} invited you to {ws_name}"
                text = (
                    f"Hi,\n\n"
                    f"{inviter_name} has invited you to join {ws_name} on "
                    f"Foundry as a workspace {body.ws_role}.\n\n"
                    f"You'll get access to:\n{org_list_text}\n\n"
                    f"Accept your invite:\n{invite_url}\n\n"
                    f"This link expires in 14 days. If you weren't expecting "
                    f"this, you can safely ignore the email.\n"
                )
                html = (
                    f"<div style='font-family:-apple-system,Segoe UI,Roboto,"
                    f"sans-serif;max-width:520px;margin:0 auto;padding:24px;"
                    f"color:#222'>"
                    f"<p style='font-size:13px;color:#888;text-transform:"
                    f"uppercase;letter-spacing:0.1em;margin:0 0 8px'>"
                    f"Workspace invitation</p>"
                    f"<h2 style='font-size:22px;margin:0 0 16px;"
                    f"line-height:1.3'>{inviter_name} has invited you to "
                    f"join <b>{ws_name}</b></h2>"
                    f"<p style='font-size:14px;line-height:1.6;margin:0 0 4px'>"
                    f"You'll join as a workspace "
                    f"<b>{body.ws_role}</b> with access to:</p>"
                    f"{org_list_html}"
                    f"<div style='margin:24px 0'>"
                    f"<a href='{invite_url}' "
                    f"style='display:inline-block;background:#f97316;"
                    f"color:#fff;padding:12px 22px;border-radius:999px;"
                    f"text-decoration:none;font-weight:600;font-size:14px'>"
                    f"Accept invitation</a></div>"
                    f"<p style='font-size:12px;color:#888;margin:16px 0 0'>"
                    f"Or copy this link: "
                    f"<a href='{invite_url}' style='color:#f97316'>"
                    f"{invite_url}</a></p>"
                    f"<p style='font-size:12px;color:#888;margin:8px 0 0'>"
                    f"This link expires in 14 days.</p>"
                    f"</div>"
                )
                import asyncio as _aio
                _aio.create_task(send_resend_email(
                    to=body.email, subject=subject, html=html, text=text,
                    reply_to=u.get("email") or None,
                ))
                logger.info(
                    "Workspace invite email queued to %s for workspace %s",
                    body.email, ws_id,
                )
            except Exception as exc:
                logger.warning("Invite email prep failed: %s", exc)
        return {
            "code": code,
            "ws_role": body.ws_role,
            "org_grants": [g.model_dump() for g in body.org_grants],
            "expires_at": doc["expires_at"].isoformat(),
            "email_sent": bool(body.email),
        }

    @r.get("/{ws_id}/invites")
    async def list_ws_invites(
        ws_id: str, u: dict = Depends(get_current_user),
    ):
        await require_workspace_member(db, ws_id, u["id"], required_role="admin")
        cur = db.workspace_invites.find(
            {"workspace_id": ws_id, "used_by": None},
        )
        out = []
        async for i in cur:
            out.append({
                "code": i.get("code"),
                "email": i.get("email", ""),
                "ws_role": i.get("ws_role", "member"),
                "org_grants": i.get("org_grants") or [],
                "expires_at": _iso(i.get("expires_at")),
            })
        return out

    @r.delete("/{ws_id}/invites/{code}")
    async def revoke_ws_invite(
        ws_id: str, code: str, u: dict = Depends(get_current_user),
    ):
        await require_workspace_member(db, ws_id, u["id"], required_role="admin")
        res = await db.workspace_invites.delete_one(
            {"workspace_id": ws_id, "code": code, "used_by": None},
        )
        return {"ok": bool(res.deleted_count)}

    return r


class WorkspaceInviteAcceptBody(BaseModel):
    code: str


def make_workspace_invite_accept_router(
    *, db, get_current_user,
) -> APIRouter:
    """Separate router so the accept URL is workspace-aware but lives
    at /api/workspace-invites/accept — not nested under a workspace id
    (the caller doesn't know it yet, they just have a code)."""
    r = APIRouter(prefix="/api/workspace-invites", tags=["workspaces"])

    @r.post("/peek")
    async def peek_invite(body: WorkspaceInviteAcceptBody):
        inv = await db.workspace_invites.find_one(
            {"code": body.code, "used_by": None},
        )
        if not inv:
            raise HTTPException(404, "Invite not found or already used")
        ws = await db.workspaces.find_one(
            {"_id": ObjectId(inv["workspace_id"])},
            {"name": 1, "owner_id": 1},
        )
        # Include Org display names so the UI can show "You'll join
        # Marketing Co, Acme Inc, etc."
        org_ids = [g["org_id"] for g in (inv.get("org_grants") or [])
                   if len(g.get("org_id", "")) == 24]
        orgs = {}
        if org_ids:
            cur = db.organizations.find(
                {"_id": {"$in": [ObjectId(i) for i in org_ids]}},
                {"name": 1},
            )
            async for o in cur:
                orgs[str(o["_id"])] = o.get("name", "")
        return {
            "workspace_id": inv["workspace_id"],
            "workspace_name": (ws or {}).get("name", ""),
            "ws_role": inv.get("ws_role", "member"),
            "org_grants": [
                {**g, "org_name": orgs.get(g.get("org_id", ""), "")}
                for g in (inv.get("org_grants") or [])
            ],
        }

    @r.post("/accept")
    async def accept_ws_invite(
        body: WorkspaceInviteAcceptBody, u: dict = Depends(get_current_user),
    ):
        inv = await db.workspace_invites.find_one(
            {"code": body.code, "used_by": None},
        )
        if not inv:
            raise HTTPException(404, "Invite not found or already used")
        exp = inv.get("expires_at")
        if isinstance(exp, datetime):
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                raise HTTPException(410, "Invite expired")
        ws_id = inv["workspace_id"]
        now = datetime.now(timezone.utc)
        # Upsert workspace_members (role from invite).
        await db.workspace_members.update_one(
            {"workspace_id": ws_id, "user_id": u["id"]},
            {"$setOnInsert": {
                "workspace_id": ws_id,
                "user_id": u["id"],
                "role": inv.get("ws_role", "member"),
                "joined_at": now,
            }},
            upsert=True,
        )
        # Add Org memberships for each grant that still belongs to this
        # workspace (grants are validated at create time, but we re-check
        # here to survive an Org being moved between workspaces later).
        # Pre-fetch the orgs in this workspace WITH names so the response
        # can tell the UI exactly which orgs the user gained access to —
        # critical when the invite re-invites an existing workspace member
        # to additional orgs (otherwise the redirect target hides which
        # orgs were just added).
        orgs_in_ws_cur = db.organizations.find(
            {"workspace_id": ws_id}, {"_id": 1, "name": 1},
        )
        orgs_in_ws_map: dict[str, str] = {}
        async for o in orgs_in_ws_cur:
            orgs_in_ws_map[str(o["_id"])] = o.get("name", "")
        first_org_id = ""
        orgs_added: list[dict[str, str]] = []
        orgs_already_member: list[dict[str, str]] = []
        for g in (inv.get("org_grants") or []):
            org_id = g.get("org_id", "")
            if org_id not in orgs_in_ws_map:
                continue
            role = g.get("role") if g.get("role") in {"admin", "member"} else "member"
            existing = await db.org_members.find_one(
                {"org_id": org_id, "user_id": u["id"]},
            )
            org_name = orgs_in_ws_map.get(org_id, "")
            if existing:
                # Upgrade role if needed, never downgrade a pre-existing owner.
                if existing.get("role") != "owner" and existing.get("role") != role:
                    await db.org_members.update_one(
                        {"_id": existing["_id"]},
                        {"$set": {"role": role}},
                    )
                orgs_already_member.append({"id": org_id, "name": org_name})
            else:
                await db.org_members.insert_one({
                    "org_id": org_id, "user_id": u["id"],
                    "role": role, "joined_at": now,
                })
                orgs_added.append({"id": org_id, "name": org_name})
            if not first_org_id:
                first_org_id = org_id
        # Mark invite used.
        await db.workspace_invites.update_one(
            {"_id": inv["_id"]}, {"$set": {"used_by": u["id"], "used_at": now}},
        )
        return {
            "workspace_id": ws_id,
            "first_org_id": first_org_id,
            "orgs_added": orgs_added,            # newly granted on this accept
            "orgs_already_member": orgs_already_member,  # already had access
        }

    return r


__all__ = [
    "make_workspaces_router",
    "make_workspace_invite_accept_router",
    "migrate_existing_orgs_to_workspaces",
    "require_workspace_member",
]
