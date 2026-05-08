"""Team visibility + Private Chief of Staff helpers.

Adds per-user-owned "private CoS" conversations and Open/Closed team
gating on top of the existing org / team / conversation model. Kept
out of `server.py` for readability — the central monolith is already
~5800 lines and growing.

New data fields (all optional / backfilled lazily so existing rows
keep working without a migration):

* `teams`:
  - `visibility`: "open" | "closed"  (default: "open" for backfill)
  - `member_user_ids`: List[str]      (default: all org members on backfill)
  - `admin_user_ids`: List[str]       (default: [created_by] on backfill)

* `conversations`:
  - `kind`: "private_cos" | …          (set on private CoS only)
  - `owner_user_id`: str                (the user this private CoS belongs to)

The org owner ALWAYS gets read access to every private CoS in their org
for audit purposes — enforced in the conv-access checks in server.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId


# ── Visibility helpers ──────────────────────────────────────────────────
def team_visibility(team: dict) -> str:
    """Returns "open" or "closed". Defaults to "open" for legacy teams
    so the new model is fully backwards compatible."""
    v = (team or {}).get("visibility")
    return v if v in ("open", "closed") else "open"


def team_member_ids(team: dict, fallback_org_member_ids: Optional[list] = None) -> list:
    """Return the user_ids that belong to this team.

    For legacy teams that pre-date the new model, we fall back to the
    list of org members the caller passes in (so the "everyone in the
    org can see this team" behaviour holds until migration).
    """
    raw = (team or {}).get("member_user_ids")
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return [str(x) for x in (fallback_org_member_ids or [])]


def team_admin_ids(team: dict) -> list:
    """Return the user_ids who can edit team settings + invite. Always
    includes the creator. Legacy teams default to creator-only."""
    raw = (team or {}).get("admin_user_ids")
    if isinstance(raw, list) and raw:
        out = [str(x) for x in raw]
    else:
        out = []
    creator = (team or {}).get("created_by")
    if creator and str(creator) not in out:
        out.append(str(creator))
    return out


def is_org_owner(org: dict, user_id: str) -> bool:
    return bool(org and str(org.get("owner_id")) == str(user_id))


def can_user_view_team(*, team: dict, org: dict, user_id: str,
                       org_member_ids: Optional[list] = None) -> bool:
    """Visibility check — does this user even see the team in their list?

    - Org owner: always True
    - General team (`is_general=True`): True for all org members
    - Open team: True for all org members
    - Closed team: True only if user is in `member_user_ids`
    """
    if is_org_owner(org, user_id):
        return True
    if team.get("is_general"):
        return str(user_id) in [str(x) for x in (org_member_ids or [])]
    if team_visibility(team) == "open":
        return str(user_id) in [str(x) for x in (org_member_ids or [])]
    # Closed team
    return str(user_id) in team_member_ids(team, org_member_ids)


def can_user_message_in_team(*, team: dict, org: dict, user_id: str,
                             org_member_ids: Optional[list] = None) -> bool:
    """Participation check — does this user have permission to actually
    send / receive messages in this team's conversations?

    Open teams require an explicit join (membership in `member_user_ids`)
    BEFORE the user can send messages, even though they can see the team
    listed. The General team is shared and never requires a join.
    """
    if is_org_owner(org, user_id):
        return True
    if team.get("is_general"):
        return str(user_id) in [str(x) for x in (org_member_ids or [])]
    return str(user_id) in team_member_ids(team, org_member_ids)


def is_team_admin(*, team: dict, org: dict, user_id: str) -> bool:
    """Team admins can change visibility + invite/remove members."""
    if is_org_owner(org, user_id):
        return True
    return str(user_id) in team_admin_ids(team)


# ── Private CoS auto-creation ────────────────────────────────────────────


PRIVATE_COS_KIND = "private_cos"


async def get_or_create_private_cos(db, *, org: dict, user_id: str,
                                     user_name: str = "") -> dict:
    """Ensure a private Chief of Staff conversation exists for this user
    in this org, then return it. Idempotent — if the conv already exists
    we return it as-is.

    Each member of the org gets exactly one private CoS thread per org
    they belong to. It lives under the org's General team for routing
    consistency (so existing "team_id → org_id" lookups Just Work).
    """
    general_team_id = org.get("general_team_id") or ""
    if not general_team_id:
        # Edge case: org provisioned without a general team (very old data)
        # — bail out cleanly so callers can ignore.
        return None

    existing = await db.conversations.find_one({
        "team_id": general_team_id,
        "kind": PRIVATE_COS_KIND,
        "owner_user_id": str(user_id),
    })
    if existing:
        return existing

    now = datetime.now(timezone.utc)
    doc = {
        "team_id": general_team_id,
        "type": "group",          # a normal multi-agent thread, just scoped to one human
        "title": "My Chief of Staff" if not user_name else f"{user_name}'s Chief of Staff",
        "kind": PRIVATE_COS_KIND,
        "owner_user_id": str(user_id),
        "agent_ids": [],
        "member_ids": [str(user_id)],
        "auto_respond": True,
        "smart_expand": True,
        "created_by": str(user_id),
        "created_at": now,
        "last_message_at": now,
    }
    res = await db.conversations.insert_one(doc)
    return await db.conversations.find_one({"_id": res.inserted_id})


async def create_new_private_cos(db, *, org: dict, user_id: str,
                                  user_name: str = "",
                                  title: str = "") -> dict:
    """Always create a fresh private Chief of Staff thread, even if the
    user already has one. Use this for the "+ New chat" affordance —
    every click should yield an empty starting point.

    The new thread is auto-titled with a short timestamp suffix so the
    list view can distinguish "your chief of staff" from "your chief
    of staff (Feb 8, 4:32 PM)" etc. — until the first user message
    triggers our regular auto-title logic.
    """
    general_team_id = org.get("general_team_id") or ""
    if not general_team_id:
        return None

    now = datetime.now(timezone.utc)
    # Title: prefer the caller-supplied one, else stamp with a short
    # human-readable timestamp so threads don't all collide on the list.
    if not title:
        base = "My Chief of Staff" if not user_name else f"{user_name}'s Chief of Staff"
        title = f"{base} · {now.strftime('%b %-d, %-I:%M %p')}"

    doc = {
        "team_id": general_team_id,
        "type": "group",
        "title": title,
        "kind": PRIVATE_COS_KIND,
        "owner_user_id": str(user_id),
        "agent_ids": [],
        "member_ids": [str(user_id)],
        "auto_respond": True,
        "smart_expand": True,
        "created_by": str(user_id),
        "created_at": now,
        "last_message_at": now,
    }
    res = await db.conversations.insert_one(doc)
    return await db.conversations.find_one({"_id": res.inserted_id})


async def list_private_cos_threads(db, *, org: dict, user_id: str,
                                    limit: int = 30) -> list[dict]:
    """Return the user's CoS threads in this org, newest-first by
    last activity. Empty list when the user has none — caller decides
    whether to auto-create one. Trimmed to `limit` so a power user
    with thousands of threads doesn't blow up the UI."""
    general_team_id = org.get("general_team_id") or ""
    if not general_team_id:
        return []
    cursor = db.conversations.find({
        "team_id": general_team_id,
        "kind": PRIVATE_COS_KIND,
        "owner_user_id": str(user_id),
    }, {
        "_id": 1, "team_id": 1, "title": 1, "kind": 1,
        "owner_user_id": 1, "created_at": 1, "last_message_at": 1,
    }).sort("last_message_at", -1).limit(int(limit))
    out = []
    async for c in cursor:
        out.append({
            "conversation_id": str(c["_id"]),
            "team_id": c.get("team_id") or general_team_id,
            "title": c.get("title") or "My Chief of Staff",
            "created_at": c.get("created_at"),
            "last_message_at": c.get("last_message_at"),
        })
    return out


def can_user_access_private_cos(*, conv: dict, org: dict, user_id: str) -> bool:
    """Private CoS conversations are visible only to:
       - The user who owns the thread (`owner_user_id`)
       - The org owner (audit access)
    """
    if conv.get("kind") != PRIVATE_COS_KIND:
        # Not a private CoS — caller should fall back to the regular
        # conversation-access check. We return None-equivalent (False)
        # to signal "not applicable to me".
        return False
    if str(conv.get("owner_user_id") or "") == str(user_id):
        return True
    if is_org_owner(org, user_id):
        return True
    return False


# ── Lazy backfill so existing data keeps working ─────────────────────────


async def backfill_team_membership(db, team: dict, org: dict) -> dict:
    """If a team is missing the new visibility / member_user_ids fields,
    backfill them in-place to keep existing behaviour: visibility=open,
    every org member is a team member, creator is the sole admin.

    Returns the team dict with the freshly-stamped fields.
    """
    needs = []
    if "visibility" not in team:
        needs.append("visibility")
    if "member_user_ids" not in team:
        needs.append("member_user_ids")
    if "admin_user_ids" not in team:
        needs.append("admin_user_ids")
    if not needs:
        return team

    org_id = str(org["_id"])
    member_rows = await db.org_members.find({"org_id": org_id}, {"user_id": 1}).to_list(2000)
    org_member_ids = [str(r["user_id"]) for r in member_rows]
    creator = str(team.get("created_by") or org.get("owner_id") or "")

    update = {}
    if "visibility" in needs:
        update["visibility"] = "open"
    if "member_user_ids" in needs:
        update["member_user_ids"] = org_member_ids
    if "admin_user_ids" in needs:
        update["admin_user_ids"] = [creator] if creator else []

    await db.teams.update_one({"_id": team["_id"]}, {"$set": update})
    team.update(update)
    return team


def serialize_team_with_visibility(team: dict, *, org: dict, viewer_id: str,
                                    org_member_ids: Optional[list] = None,
                                    last_message_at: Optional[datetime] = None) -> dict:
    """Extended team payload with the new fields, plus per-viewer
    booleans (is_member / is_admin / can_join) so the frontend can
    render the right pill + Join button without a second roundtrip.
    """
    out = {
        "id": str(team["_id"]),
        "org_id": team["org_id"],
        "name": team.get("name", ""),
        "description": team.get("description", ""),
        "soulprint": team.get("soulprint", {}),
        "is_general": bool(team.get("is_general")),
        "is_direct": bool(team.get("is_direct")),
        "visibility": team_visibility(team),
        "member_user_ids": team_member_ids(team, org_member_ids),
        "admin_user_ids": team_admin_ids(team),
        "created_by": str(team.get("created_by") or ""),
        "created_at": team["created_at"].isoformat() if isinstance(team.get("created_at"), datetime) else team.get("created_at"),
    }
    out["is_member"] = can_user_message_in_team(
        team=team, org=org, user_id=viewer_id, org_member_ids=org_member_ids,
    )
    out["is_admin"] = is_team_admin(team=team, org=org, user_id=viewer_id)
    # Open teams the user hasn't joined → can_join=True so the UI
    # surfaces a "Join team" button instead of a chat composer.
    out["can_join"] = (
        not out["is_member"]
        and out["visibility"] == "open"
        and not bool(team.get("is_general"))
    )
    if last_message_at is not None:
        out["last_message_at"] = (
            last_message_at.isoformat() if isinstance(last_message_at, datetime)
            else last_message_at
        )
    return out
