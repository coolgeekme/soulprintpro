"""Soul Suggestions — member-facing change requests against an org's Soul.

Policy (per business owner directive, Feb 2026):
- Only an org **owner** can directly mutate the company Soul (handled in
  ``server.update_org`` via role-check).
- Any **member** (including admins and the owner themselves) can submit a
  ``soul_suggestion`` — a structured proposal that the owner/admin team
  reviews and either approves (applies to ``organizations.soul``) or
  rejects.
- Members see only their own suggestions; owners/admins see them all.
- Approving a suggestion stamps the *suggested fields only* onto the org's
  Soul — fields the suggester left blank are preserved as-is.

Mongo collection: ``soul_suggestions``. One doc per suggestion; never
mutated after review (we keep the audit trail).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel


# Soul field whitelist — must mirror the keys produced by
# server._generate_company_soul. Anything outside this set is silently
# dropped so callers can't smuggle arbitrary org-level fields through a
# suggestion.
_SOUL_FIELDS = {"mission", "tone", "audience", "values", "north_star", "summary"}


class SoulSuggestionBody(BaseModel):
    # Each field is optional — submitter only fills what they want to change.
    mission: Optional[str] = None
    tone: Optional[str] = None
    audience: Optional[str] = None
    values: Optional[str] = None
    north_star: Optional[str] = None
    summary: Optional[str] = None
    note: Optional[str] = ""  # free-text rationale for the change


class ReviewBody(BaseModel):
    note: Optional[str] = ""  # owner/admin's reviewer note (visible to submitter)


def _serialize(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "org_id": doc["org_id"],
        "submitted_by_user_id": doc["submitted_by_user_id"],
        "submitted_by_name": doc.get("submitted_by_name", ""),
        "submitted_by_email": doc.get("submitted_by_email", ""),
        "fields": doc.get("fields", {}),
        "note": doc.get("note", ""),
        "status": doc.get("status", "pending"),
        "reviewed_by_user_id": doc.get("reviewed_by_user_id"),
        "reviewed_by_name": doc.get("reviewed_by_name", ""),
        "reviewer_note": doc.get("reviewer_note", ""),
        "created_at": doc["created_at"].isoformat() if isinstance(doc.get("created_at"), datetime) else doc.get("created_at"),
        "reviewed_at": doc["reviewed_at"].isoformat() if isinstance(doc.get("reviewed_at"), datetime) else doc.get("reviewed_at"),
    }


def make_soul_suggestions_router(*, db, require_org_member, get_current_user) -> APIRouter:
    r = APIRouter(prefix="/api/orgs/{org_id}/soul-suggestions")

    @r.post("")
    async def submit_suggestion(
        org_id: str,
        body: SoulSuggestionBody,
        u: dict = Depends(get_current_user),
    ):
        """Any org member can propose changes to the company Soul. The
        suggestion sits in `pending` until an owner/admin reviews it."""
        await require_org_member(org_id, u["id"])  # 403s non-members

        # Build the fields dict from non-empty proposed values only.
        proposed = {}
        for k in _SOUL_FIELDS:
            v = getattr(body, k, None)
            if v is not None and str(v).strip():
                proposed[k] = str(v).strip()
        if not proposed:
            raise HTTPException(
                422,
                "Suggestion is empty — fill in at least one Soul field "
                "(mission / tone / audience / values / north star / summary).",
            )
        # Reject identical suggestions (same fields → same values) that are still pending.
        existing = await db.soul_suggestions.find_one({
            "org_id": org_id,
            "submitted_by_user_id": u["id"],
            "status": "pending",
            "fields": proposed,
        })
        if existing:
            raise HTTPException(409, "You already have an identical pending suggestion.")

        doc = {
            "org_id": org_id,
            "submitted_by_user_id": u["id"],
            "submitted_by_name": u.get("name", ""),
            "submitted_by_email": u.get("email", ""),
            "fields": proposed,
            "note": (body.note or "").strip(),
            "status": "pending",
            "reviewed_by_user_id": None,
            "reviewed_by_name": "",
            "reviewer_note": "",
            "created_at": datetime.now(timezone.utc),
            "reviewed_at": None,
        }
        res = await db.soul_suggestions.insert_one(doc)
        doc["_id"] = res.inserted_id
        return _serialize(doc)

    @r.get("")
    async def list_suggestions(
        org_id: str,
        status: Optional[str] = None,  # filter by pending/approved/rejected/cancelled
        u: dict = Depends(get_current_user),
    ):
        """Owners/admins see every suggestion in the org; members see only
        their own. Sorted by most recent first."""
        ctx = await require_org_member(org_id, u["id"])
        is_reviewer = ctx["member"]["role"] in ("owner", "admin")
        q: dict = {"org_id": org_id}
        if not is_reviewer:
            q["submitted_by_user_id"] = u["id"]
        if status:
            q["status"] = status
        docs = await db.soul_suggestions.find(q).sort("created_at", -1).to_list(200)
        return [_serialize(d) for d in docs]

    async def _load_pending_or_404(org_id: str, suggestion_id: str) -> dict:
        try:
            sid = ObjectId(suggestion_id)
        except Exception as exc:
            raise HTTPException(400, "Invalid suggestion id") from exc
        doc = await db.soul_suggestions.find_one({"_id": sid, "org_id": org_id})
        if not doc:
            raise HTTPException(404, "Suggestion not found")
        return doc

    @r.post("/{suggestion_id}/approve")
    async def approve_suggestion(
        org_id: str,
        suggestion_id: str,
        body: ReviewBody = Body(default=ReviewBody()),
        u: dict = Depends(get_current_user),
    ):
        """Owner/admin: apply the suggestion's fields onto the org's Soul.
        Only fields the submitter actually filled in are touched — empty
        fields are preserved as-is."""
        ctx = await require_org_member(org_id, u["id"])
        if ctx["member"]["role"] not in ("owner", "admin"):
            raise HTTPException(403, "Only owners/admins can approve suggestions")
        doc = await _load_pending_or_404(org_id, suggestion_id)
        if doc["status"] != "pending":
            raise HTTPException(409, f"Suggestion already {doc['status']}")

        # Merge proposed fields into the existing soul dict.
        org = ctx["org"]
        current_soul = dict(org.get("soul") or {})
        proposed = doc.get("fields") or {}
        current_soul.update(proposed)

        # Cascade fresh Soul into the auto-created "General" team's
        # soulprint snapshot so its agents pick up the new mission/tone
        # immediately (mirrors the cascade in server.update_org).
        await db.organizations.update_one(
            {"_id": ObjectId(org_id)},
            {"$set": {"soul": current_soul}},
        )
        general_team_id = org.get("general_team_id")
        if general_team_id:
            try:
                await db.teams.update_one(
                    {"_id": ObjectId(general_team_id)},
                    {"$set": {
                        "soulprint": {
                            "mission": current_soul.get("mission", ""),
                            "tone": current_soul.get("tone", ""),
                            "audience": current_soul.get("audience", ""),
                            "values": current_soul.get("values", ""),
                            "north_star": current_soul.get("north_star", ""),
                        }
                    }},
                )
            except Exception:
                pass

        await db.soul_suggestions.update_one(
            {"_id": doc["_id"]},
            {"$set": {
                "status": "approved",
                "reviewed_by_user_id": u["id"],
                "reviewed_by_name": u.get("name", ""),
                "reviewer_note": (body.note or "").strip(),
                "reviewed_at": datetime.now(timezone.utc),
            }},
        )
        fresh = await db.soul_suggestions.find_one({"_id": doc["_id"]})
        return _serialize(fresh)

    @r.post("/{suggestion_id}/reject")
    async def reject_suggestion(
        org_id: str,
        suggestion_id: str,
        body: ReviewBody = Body(default=ReviewBody()),
        u: dict = Depends(get_current_user),
    ):
        ctx = await require_org_member(org_id, u["id"])
        if ctx["member"]["role"] not in ("owner", "admin"):
            raise HTTPException(403, "Only owners/admins can reject suggestions")
        doc = await _load_pending_or_404(org_id, suggestion_id)
        if doc["status"] != "pending":
            raise HTTPException(409, f"Suggestion already {doc['status']}")
        await db.soul_suggestions.update_one(
            {"_id": doc["_id"]},
            {"$set": {
                "status": "rejected",
                "reviewed_by_user_id": u["id"],
                "reviewed_by_name": u.get("name", ""),
                "reviewer_note": (body.note or "").strip(),
                "reviewed_at": datetime.now(timezone.utc),
            }},
        )
        fresh = await db.soul_suggestions.find_one({"_id": doc["_id"]})
        return _serialize(fresh)

    @r.delete("/{suggestion_id}")
    async def cancel_suggestion(
        org_id: str,
        suggestion_id: str,
        u: dict = Depends(get_current_user),
    ):
        """Submitter can cancel their own pending suggestion. Owners/admins
        can delete any suggestion (e.g. spam cleanup)."""
        ctx = await require_org_member(org_id, u["id"])
        doc = await _load_pending_or_404(org_id, suggestion_id)
        is_reviewer = ctx["member"]["role"] in ("owner", "admin")
        is_author = doc["submitted_by_user_id"] == u["id"]
        if not (is_author or is_reviewer):
            raise HTTPException(403, "You can only cancel your own suggestions")
        if is_author and not is_reviewer and doc["status"] != "pending":
            raise HTTPException(
                409,
                f"Cannot cancel — suggestion has already been {doc['status']}",
            )
        await db.soul_suggestions.delete_one({"_id": doc["_id"]})
        return {"deleted": True}

    return r
