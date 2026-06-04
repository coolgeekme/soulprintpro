"""GDPR-friendly data export + right-to-erasure flows.

Two user-facing features, both reachable from the in-product
``Account → Privacy`` panel:

  • **Export my data** — `POST /api/account/export`. Kicks off a
    background ZIP-builder job; the user polls `GET /api/account/export`
    for status and downloads via a one-time signed URL when ready.

  • **Delete my account** — `POST /api/account/delete`. Schedules hard
    deletion 30 days out (grace period — handles "I clicked at 2 AM"
    cases). The user can cancel any time during the window with
    `POST /api/account/delete/cancel`. A daily APScheduler sweep runs
    the actual hard delete when the grace expires.

Edge cases the module handles explicitly:

  • **Ownership transfer is mandatory** when the requester is the sole
    Owner of any org with other members. `GET /api/account/delete/status`
    surfaces blockers and the available transfer targets; the user
    picks an Admin to promote to Owner via
    `POST /api/account/delete/transfer-ownership`. Deletion is blocked
    until every org the user owns is either transferred or sole-owner-
    with-no-other-members (in which case it cascade-deletes with them).

  • **Team-visible thread integrity**. Messages the requester wrote in
    team threads are anonymised (sender_name → "[deleted user]",
    sender_id → null) rather than hard-deleted, preserving thread
    coherence for other members. SOC 2 + legal-hold also expects this.

  • **Retention exceptions** — Stripe billing records (tax compliance),
    auth audit logs (12 months for security investigations), aggregate
    analytics (counts, sums, no PII) are NOT deleted. Documented in
    the privacy policy.

  • **Composio revocation** — every Composio connection the user owns
    is revoked through Composio's API as part of hard delete, so we
    don't leave dangling OAuth tokens.

The module is intentionally self-contained — `make_privacy_router()` is
the only public surface — so the next agent can refactor or unbundle
without ripple effects through `server.py`.
"""
from __future__ import annotations

import io
import json
import logging
import os
import secrets
import shutil
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException
from fastapi.responses import FileResponse

logger = logging.getLogger("foundry.privacy")

# Where the built ZIPs land. We keep them on disk for a bounded window
# (24h by default) — the user gets a one-time signed URL via the
# polling endpoint and the cleanup sweep removes expired files.
_EXPORT_DIR = Path(os.environ.get("PRIVACY_EXPORT_DIR", "/app/backend/exports"))
_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
_EXPORT_TTL = timedelta(hours=24)
# Grace window between a user requesting deletion and the hard delete
# actually running. 30 days mirrors GDPR right-to-be-forgotten best
# practice and gives operators a generous "I changed my mind" buffer.
_DELETE_GRACE = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _safe_slug(text: str, *, max_len: int = 32) -> str:
    """Filesystem-safe slug for org names + folder paths inside the ZIP."""
    import re
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", text or "").strip("-")
    return (s or "untitled")[:max_len].lower()


# ── User-data collection ────────────────────────────────────────────────────


async def _collect_orgs_for_user(db, user_id: str) -> List[Dict[str, Any]]:
    """Every org the user is a member of, with their role + role grant date."""
    out: List[Dict[str, Any]] = []
    cur = db.org_members.find({"user_id": user_id})
    async for m in cur:
        try:
            org = await db.organizations.find_one(
                {"_id": ObjectId(m["org_id"])},
                {"_id": 1, "name": 1, "description": 1, "created_at": 1, "general_team_id": 1},
            )
        except Exception:
            continue
        if not org:
            continue
        out.append({
            "org_id": str(org["_id"]),
            "name": org.get("name", ""),
            "description": org.get("description", ""),
            "role": m.get("role", "member"),
            "joined_at": _iso(m.get("created_at") or m.get("joined_at")),
            "org_created_at": _iso(org.get("created_at")),
        })
    return out


async def _find_blocking_owned_orgs(db, user_id: str) -> List[Dict[str, Any]]:
    """Orgs where the user is THE sole Owner AND there is at least one
    other member. These block deletion until ownership is transferred
    (or the user explicitly opts to cascade-delete the org).

    Returns one dict per blocking org with the list of transfer
    targets (Admins / Members the user could promote).
    """
    blocking: List[Dict[str, Any]] = []
    cur = db.org_members.find({"user_id": user_id, "role": "owner"})
    async for m in cur:
        org_id = m["org_id"]
        owners = await db.org_members.count_documents({"org_id": org_id, "role": "owner"})
        total = await db.org_members.count_documents({"org_id": org_id})
        if owners > 1:
            continue  # another owner exists; this org isn't blocking
        if total <= 1:
            continue  # sole member — will cascade-delete with the user
        try:
            org = await db.organizations.find_one({"_id": ObjectId(org_id)}, {"_id": 1, "name": 1})
        except Exception:
            org = None
        # Transfer-target candidates: Admins first, then Members.
        targets_cur = db.org_members.find({
            "org_id": org_id, "user_id": {"$ne": user_id},
        })
        targets: List[Dict[str, Any]] = []
        async for t in targets_cur:
            try:
                u = await db.users.find_one(
                    {"_id": ObjectId(t["user_id"])},
                    {"_id": 0, "name": 1, "email": 1},
                )
            except Exception:
                u = None
            if not u:
                continue
            targets.append({
                "user_id": t["user_id"],
                "name": u.get("name", ""),
                "email": u.get("email", ""),
                "role": t.get("role", "member"),
            })
        # Stable ordering: Admins, then alphabetical by email.
        targets.sort(key=lambda x: (0 if x["role"] == "admin" else 1, x["email"]))
        blocking.append({
            "org_id": org_id,
            "org_name": (org or {}).get("name", "(unknown)"),
            "member_count": total,
            "transfer_targets": targets,
        })
    return blocking


# ── ZIP builder ─────────────────────────────────────────────────────────────


async def _build_export_zip(db, user_id: str, job_id: str) -> Path:
    """Construct the user's personal data ZIP and return the path to it.

    Structure (see README inside the ZIP for the full contract):

        foundry-export-<user>-<ts>.zip
        ├── README.md
        ├── account.json
        ├── memory/                # cross-org user memory
        ├── connections.json       # Composio connections (no tokens)
        ├── orgs/
        │   ├── <org_slug>/
        │   │   ├── role.json
        │   │   ├── conversations/
        │   │   │   ├── direct/<conv>.json     # user's DMs / private CoS
        │   │   │   └── team-authored.json     # only messages YOU wrote
        │   │   ├── documents/<file>           # docs the user uploaded
        │   │   └── soulprints.json            # only if Owner/Admin
        │   └── ...

    Hard rules baked in:
      • Other members' messages in team threads are NEVER exported.
        We export only messages where `sender_id == user_id`, with a
        redacted-context-line note in the README so the user knows.
      • Documents are included only when `uploaded_by == user_id`.
      • Composio tokens are stripped before serialisation.
    """
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise ValueError(f"user {user_id} not found")
    ts = _now().strftime("%Y%m%d-%H%M%S")
    zip_path = _EXPORT_DIR / f"foundry-export-{user_id[:8]}-{ts}-{job_id[:8]}.zip"

    orgs = await _collect_orgs_for_user(db, user_id)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # ── README ───────────────────────────────────────────────────
        zf.writestr("README.md", _build_readme(user, orgs))

        # ── account.json ─────────────────────────────────────────────
        account = {
            "user_id": user_id,
            "name": user.get("name"),
            "email": user.get("email"),
            "phone": user.get("phone"),
            "timezone": user.get("timezone"),
            "firebase_uid": user.get("firebase_uid"),
            "telegram_chat_id": user.get("telegram_chat_id"),
            "telegram_username": user.get("telegram_username"),
            "created_at": _iso(user.get("created_at")),
            "is_superadmin": bool(user.get("is_superadmin")),
        }
        zf.writestr("account.json", json.dumps(account, indent=2, default=str))

        # ── cross-org user memory ───────────────────────────────────
        mem_cur = db.user_memory.find({"user_id": user_id})
        mems = []
        async for m in mem_cur:
            m.pop("_id", None)
            mems.append(m)
        zf.writestr("memory/user_memory.json", json.dumps(mems, indent=2, default=str))

        # ── Composio connections (no tokens) ────────────────────────
        # `composio_user_id` is org-scoped, so we look up every
        # connection across every org membership.
        conns_out: List[Dict[str, Any]] = []
        for o in orgs:
            cu = f"foundry-{o['org_id']}-{user_id}"
            conns_cur = db.composio_connections.find({"composio_user_id": cu})
            async for c in conns_cur:
                # Strip anything that smells like a credential.
                safe = {k: v for k, v in c.items() if k not in {
                    "_id", "access_token", "refresh_token", "auth_config",
                    "credentials", "secrets",
                }}
                safe["org_id"] = o["org_id"]
                conns_out.append(safe)
        zf.writestr("connections.json", json.dumps(conns_out, indent=2, default=str))

        # ── per-org folders ──────────────────────────────────────────
        for o in orgs:
            org_id = o["org_id"]
            base = f"orgs/{_safe_slug(o['name'])}-{org_id[:6]}"
            zf.writestr(f"{base}/role.json", json.dumps(o, indent=2, default=str))

            # Conversations:
            #   1. user is owner of the conv (DM, private CoS) → full export
            #   2. user authored messages in a team conv → only their messages
            convs_cur = db.conversations.find({
                "$or": [
                    {"owner_user_id": user_id},
                    # Team threads where user is a participant — we'll
                    # filter messages below.
                ],
            })
            owned_convs: List[Dict[str, Any]] = []
            async for c in convs_cur:
                # Scope to this org via the team_id → org_id linkage.
                team_id = c.get("team_id")
                if not team_id:
                    continue
                try:
                    team = await db.teams.find_one({"_id": ObjectId(team_id)}, {"_id": 0, "org_id": 1})
                except Exception:
                    team = None
                if not team or team.get("org_id") != org_id:
                    continue
                c.pop("_id", None)
                owned_convs.append(c)

            for c in owned_convs:
                cid = c.get("id") or str(c.get("_id") or uuid.uuid4())
                # Pull all messages in this conv (it's user-owned, so
                # everything in it is the user's data).
                msg_cur = db.messages.find({"conversation_id": cid})
                msgs = []
                async for m in msg_cur:
                    m.pop("_id", None)
                    msgs.append(m)
                zf.writestr(
                    f"{base}/conversations/direct/{cid[:30]}.json",
                    json.dumps({"conversation": c, "messages": msgs}, indent=2, default=str),
                )

            # Team-thread messages the user authored (no conv body,
            # just the user's contributions). We scope to messages
            # with sender_id == user_id and a team_id matching this
            # org — the join goes through the conversation doc.
            team_msgs: List[Dict[str, Any]] = []
            msg_cur = db.messages.find({"sender_id": user_id, "sender_type": "user"}).limit(5000)
            async for m in msg_cur:
                conv = await db.conversations.find_one(
                    {"id": m.get("conversation_id")},
                    {"_id": 0, "team_id": 1, "owner_user_id": 1, "kind": 1},
                )
                if not conv:
                    continue
                # Skip private-CoS messages — they're already in the
                # `direct/` folder above. Only capture team-thread.
                if conv.get("owner_user_id") == user_id:
                    continue
                try:
                    team = await db.teams.find_one(
                        {"_id": ObjectId(conv.get("team_id"))},
                        {"_id": 0, "org_id": 1},
                    )
                except Exception:
                    team = None
                if not team or team.get("org_id") != org_id:
                    continue
                m.pop("_id", None)
                team_msgs.append(m)
            if team_msgs:
                zf.writestr(
                    f"{base}/conversations/team-authored.json",
                    json.dumps(team_msgs, indent=2, default=str),
                )

            # Documents the user uploaded.
            docs_cur = db.documents.find({"org_id": org_id, "created_by": user_id})
            docs_meta = []
            async for d in docs_cur:
                d.pop("_id", None)
                docs_meta.append({k: v for k, v in d.items() if k not in {"gridfs_id"}})
            zf.writestr(
                f"{base}/documents/_manifest.json",
                json.dumps(docs_meta, indent=2, default=str),
            )

            # SoulPrints — only if the user is Owner/Admin in this org.
            if o["role"] in ("owner", "admin"):
                teams_cur = db.teams.find({"org_id": org_id})
                sp_out = []
                async for t in teams_cur:
                    sp_out.append({
                        "team_id": str(t["_id"]),
                        "name": t.get("name"),
                        "soulprint": t.get("soulprint") or {},
                    })
                zf.writestr(f"{base}/soulprints.json", json.dumps(sp_out, indent=2, default=str))

    return zip_path


def _build_readme(user: Dict[str, Any], orgs: List[Dict[str, Any]]) -> str:
    return (
        "# Foundry — Personal Data Export\n\n"
        f"Generated for: **{user.get('name') or user.get('email')}** (`{user.get('email')}`)\n"
        f"Generated at: **{_now().isoformat()}**\n"
        f"Orgs included: **{len(orgs)}**\n\n"
        "## What's in this archive\n\n"
        "This ZIP contains every piece of personal data Foundry stores "
        "on you, as required by GDPR Article 15 (right of access) and "
        "Article 20 (right of portability). All data is in JSON.\n\n"
        "### Per-user files\n\n"
        "- `account.json` — your profile (name, email, phone, timezone, "
        "Telegram link). No password hash is included.\n"
        "- `memory/user_memory.json` — your cross-org personal memory "
        "(preferences, defaults). Not org-scoped.\n"
        "- `connections.json` — every third-party integration you've "
        "connected (Gmail, HubSpot, etc.). **OAuth tokens are stripped** "
        "for security; only metadata (toolkit, status, connected_at) is "
        "included.\n\n"
        "### Per-org folders (`orgs/<org-name>/`)\n\n"
        "- `role.json` — your role + when you joined.\n"
        "- `conversations/direct/` — every conversation you OWN (DMs "
        "with agents, your private Chief of Staff threads). Full "
        "message history included because every message in these "
        "conversations is yours.\n"
        "- `conversations/team-authored.json` — messages you wrote in "
        "team-visible threads. **Other members' messages are NOT "
        "included** to protect their data rights. If you need the full "
        "team-thread context, ask the team owner — they can export the "
        "team's data with appropriate authorisation.\n"
        "- `documents/_manifest.json` — metadata for documents you "
        "uploaded. The binaries are not bundled here; download from "
        "the Documents library before requesting account deletion.\n"
        "- `soulprints.json` — included only for Orgs where you're "
        "Owner or Admin (team SoulPrints are org-level data).\n\n"
        "## What is NOT included (and why)\n\n"
        "- **Other members' messages** in team threads, even though "
        "you can see them in the app. They belong to the authors, "
        "not to you.\n"
        "- **Audit logs containing your auth events** — kept for "
        "12 months for security investigations (legitimate-interest "
        "basis under GDPR Article 6(1)(f)).\n"
        "- **Stripe billing records** — retained for tax compliance "
        "(typically 7 years, jurisdiction-dependent).\n"
        "- **Aggregated analytics** — counts, sums, etc. that don't "
        "contain PII. Anonymised data is out of scope for GDPR.\n\n"
        "## To request deletion\n\n"
        "Visit `Account → Privacy → Delete my account` in the Foundry "
        "app. Deletion takes 30 days from request (grace period); you "
        "can cancel any time during that window with a single click.\n\n"
        "If you owned any orgs, ownership must be transferred to an "
        "Admin or the org must be explicitly deleted before your "
        "account can be removed.\n"
    )


# ── Background jobs ─────────────────────────────────────────────────────────


def _make_token() -> str:
    """One-time download token. We don't sign it because it's already a
    long random value that's invalidated on first use server-side."""
    return secrets.token_urlsafe(32)


async def _run_export_job(db, user_id: str, job_id: str) -> None:
    """Background-task body — builds the ZIP, then updates the job
    row with the download token + path. Errors update the row too so
    the polling endpoint can surface them to the user."""
    try:
        zip_path = await _build_export_zip(db, user_id, job_id)
        token = _make_token()
        await db.privacy_export_jobs.update_one(
            {"id": job_id},
            {"$set": {
                "status": "ready",
                "zip_path": str(zip_path),
                "size_bytes": zip_path.stat().st_size,
                "token": token,
                "ready_at": _now(),
                "expires_at": _now() + _EXPORT_TTL,
            }},
        )
        logger.info("Privacy export ready: user=%s job=%s size=%s",
                    user_id, job_id, zip_path.stat().st_size)
    except Exception as exc:
        logger.exception("Privacy export failed for user=%s job=%s", user_id, job_id)
        await db.privacy_export_jobs.update_one(
            {"id": job_id},
            {"$set": {"status": "failed", "error": str(exc)[:500], "failed_at": _now()}},
        )


async def sweep_expired_exports(db) -> None:
    """Daily housekeeping job. Deletes ZIP files whose `expires_at` has
    passed and marks the row `expired`. Wired into APScheduler from
    server.py at startup."""
    cur = db.privacy_export_jobs.find({
        "status": "ready",
        "expires_at": {"$lt": _now()},
    })
    async for j in cur:
        p = j.get("zip_path")
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("Failed to delete expired export %s: %s", p, exc)
        await db.privacy_export_jobs.update_one(
            {"_id": j["_id"]},
            {"$set": {"status": "expired", "zip_path": None, "token": None}},
        )


# ── Hard-delete sweep ───────────────────────────────────────────────────────


async def run_pending_deletions(db) -> None:
    """Daily APScheduler job — runs the actual hard delete for any
    user whose grace period has elapsed. Idempotent; a user that's
    already been hard-deleted simply won't match the query.
    """
    cur = db.users.find({
        "deletion_requested_at": {"$lt": _now() - _DELETE_GRACE},
        "deletion_status": "pending",
    })
    async for u in cur:
        uid = str(u["_id"])
        try:
            await _hard_delete_user(db, uid)
            logger.info("Hard-deleted user %s after grace period", uid)
        except Exception:
            logger.exception("Hard-delete failed for user %s", uid)


async def _hard_delete_user(db, user_id: str) -> None:
    """Execute the full hard delete. Steps in order:
      1. Revoke every Composio connection the user owns (best-effort).
      2. Cascade-delete every solo-owned org (sole member or no other
         owners + user opted-in via the wizard) via the shared
         ``org_cleanup.purge_org_data`` helper — that takes care of
         disk + GridFS byte cleanup, all metadata collections, and
         upstream Composio revocation.
      3. Hard-delete private conversations + their messages + the
         per-conversation upload directories.
      4. Anonymise the user's messages in team-visible threads.
      5. Delete user_memory.
      6. Delete the user's org_members rows + any pending invites.
      7. Delete the user's privacy_export_jobs ZIPs + rows.
      8. Delete the auth_audit_log entries OLDER than the 12-month
         retention window; keep the rest (security-investigations basis).
      9. Delete the users row itself.
    """
    # 1. Revoke any USER-scoped Composio connections (e.g. Telegram
    #    Bot tokens) that aren't tied to a specific org. Org-scoped
    #    connections are handled by purge_org_data in step 2.
    try:
        from composio_integration import is_available, get_client  # type: ignore
        client = get_client() if is_available() else None
        conns_cur = db.composio_connections.find({
            "composio_user_id": {"$regex": f"-{user_id}$"},
        })
        async for c in conns_cur:
            try:
                if client and c.get("connection_id"):
                    client.connected_accounts.delete(c["connection_id"])
            except Exception as exc:
                logger.warning("Composio revoke failed for %s: %s", c.get("connection_id"), exc)
        await db.composio_connections.delete_many({
            "composio_user_id": {"$regex": f"-{user_id}$"},
        })
    except Exception as exc:
        logger.warning("Composio revoke phase failed (continuing): %s", exc)

    # 2. Cascade-delete solo-owned orgs via the shared purge helper.
    from org_cleanup import purge_org_data
    upload_dir = _upload_dir_from_env()
    assets_dir = _assets_dir_from_env()
    docs_dir = _docs_dir_from_env()
    conv_upload_dir = _conv_upload_dir_from_env()
    owned_cur = db.org_members.find({"user_id": user_id, "role": "owner"})
    async for m in owned_cur:
        org_id = m["org_id"]
        total = await db.org_members.count_documents({"org_id": org_id})
        if total > 1:
            # Ownership transfer should have happened during the wizard.
            # If another owner already exists at this point the org has
            # continuity — skip the cascade and just leave it.
            continue
        try:
            stats = await purge_org_data(
                db, org_id,
                upload_dir=upload_dir,
                assets_dir=assets_dir,
                docs_dir=docs_dir,
                conv_upload_dir=conv_upload_dir,
                # Org-scoped Composio rows are revoked by the helper.
                revoke_composio=True,
            )
            logger.info("Privacy purge for org=%s (user=%s): %s",
                        org_id, user_id, stats)
        except Exception as exc:
            logger.error(
                "Privacy purge of solo-owned org %s for user %s failed: %s",
                org_id, user_id, exc,
            )

    # 3. Hard-delete private conversations + their messages + their
    #    per-conversation upload subdirectories.
    own_convs_cur = db.conversations.find({"owner_user_id": user_id})
    own_conv_ids: List[str] = []
    async for c in own_convs_cur:
        own_conv_ids.append(c.get("id") or str(c.get("_id")))
    if own_conv_ids:
        await db.messages.delete_many({"conversation_id": {"$in": own_conv_ids}})
        await db.conversations.delete_many({"owner_user_id": user_id})
        # Per-conversation upload directories (user-attached images,
        # PDFs, audio notes). Each conversation owns its own subfolder.
        if conv_upload_dir and conv_upload_dir.exists():
            for cid in own_conv_ids:
                if not cid:
                    continue
                sub = conv_upload_dir / cid
                if sub.exists() and sub.is_dir():
                    try:
                        shutil.rmtree(sub, ignore_errors=True)
                    except Exception:
                        pass

    # 4. Anonymise team-thread messages.
    await db.messages.update_many(
        {"sender_id": user_id, "sender_type": "user"},
        {"$set": {
            "sender_id": None,
            "sender_name": "[deleted user]",
            "anonymised_at": _now(),
        }},
    )

    # 5. user_memory.
    await db.user_memory.delete_many({"user_id": user_id})
    try:
        await db.user_memories.delete_many({"user_id": user_id})
    except Exception:
        pass

    # 6. org_members + invites.
    await db.org_members.delete_many({"user_id": user_id})
    await db.invites.delete_many({"$or": [
        {"invited_by": user_id},
        {"redeemed_by": user_id},
    ]})

    # 7. Privacy export ZIPs.
    jobs_cur = db.privacy_export_jobs.find({"user_id": user_id})
    async for j in jobs_cur:
        p = j.get("zip_path")
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
    await db.privacy_export_jobs.delete_many({"user_id": user_id})

    # 8. Auth audit log — keep within retention; delete older.
    retention_cutoff = _now() - timedelta(days=365)
    await db.auth_audit_log.delete_many({
        "user_id": user_id,
        "at": {"$lt": retention_cutoff},
    })

    # 9. The users row itself.
    await db.users.delete_one({"_id": ObjectId(user_id)})


def _upload_dir_from_env() -> Path:
    """Resolve the UPLOAD_DIR the rest of the app uses. We mirror the
    same single source — server.py — so the cleanup hits the same
    paths the writes used. Falls back to the conventional location."""
    root = Path(os.environ.get("BACKEND_ROOT", "/app/backend"))
    return root / "uploads"


def _assets_dir_from_env() -> Path:
    return _upload_dir_from_env() / "assets"


def _docs_dir_from_env() -> Path:
    return _upload_dir_from_env() / "docs"


def _conv_upload_dir_from_env() -> Path:
    return _upload_dir_from_env() / "conv"


# ── Router ──────────────────────────────────────────────────────────────────


def make_privacy_router(*, get_current_user, db) -> APIRouter:
    """Mount privacy endpoints. Caller injects ``get_current_user`` to
    avoid circular imports with server.py.
    """
    r = APIRouter(prefix="/api/account")

    @r.post("/export")
    async def request_export(
        background_tasks: BackgroundTasks,
        u: dict = Depends(get_current_user),
    ):
        """Kick off a background ZIP build. Returns the job id; the
        client polls `GET /api/account/export` to check status."""
        uid = u["id"]
        # If there's a still-building or recently-ready job, return that
        # one instead of starting a duplicate.
        existing = await db.privacy_export_jobs.find_one(
            {"user_id": uid, "status": {"$in": ["building", "ready"]}},
            sort=[("requested_at", -1)],
        )
        if existing:
            exp = existing.get("expires_at")
            if exp is not None and exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if existing.get("status") == "ready" and (exp or _now()) > _now():
                existing.pop("_id", None)
                # Don't leak the download token in the status payload —
                # only `/api/account/export/download/<token>` consumes it.
                existing.pop("token", None)
                existing["ready_at"] = _iso(existing.get("ready_at"))
                existing["expires_at"] = _iso(existing.get("expires_at"))
                existing["requested_at"] = _iso(existing.get("requested_at"))
                return existing
            if existing.get("status") == "building":
                return {"id": existing["id"], "status": "building",
                        "requested_at": _iso(existing.get("requested_at"))}
        job_id = str(uuid.uuid4())
        await db.privacy_export_jobs.insert_one({
            "id": job_id,
            "user_id": uid,
            "status": "building",
            "requested_at": _now(),
        })
        background_tasks.add_task(_run_export_job, db, uid, job_id)
        return {"id": job_id, "status": "building", "requested_at": _iso(_now())}

    @r.get("/export")
    async def get_latest_export(u: dict = Depends(get_current_user)):
        """Return the latest export job for this user (status + a
        relative download URL when ready). Token is NOT included; the
        download URL embeds it."""
        job = await db.privacy_export_jobs.find_one(
            {"user_id": u["id"]},
            sort=[("requested_at", -1)],
        )
        if not job:
            return {"status": "none"}
        out = {
            "id": job.get("id"),
            "status": job.get("status"),
            "requested_at": _iso(job.get("requested_at")),
            "ready_at": _iso(job.get("ready_at")),
            "expires_at": _iso(job.get("expires_at")),
            "size_bytes": job.get("size_bytes"),
            "error": job.get("error"),
        }
        token = job.get("token")
        if job.get("status") == "ready" and token:
            out["download_url"] = f"/api/account/export/download/{token}"
        return out

    @r.get("/export/download/{token}")
    async def download_export(token: str, u: dict = Depends(get_current_user)):
        """Stream the ZIP. The token is single-use: we invalidate it
        as soon as the file response is constructed. (Re-downloading
        requires re-requesting the export.)
        """
        job = await db.privacy_export_jobs.find_one({"token": token, "user_id": u["id"]})
        if not job:
            raise HTTPException(404, "Download not found or already consumed")
        if job.get("status") != "ready":
            raise HTTPException(409, f"Export status: {job.get('status')}")
        exp = job.get("expires_at")
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp and exp < _now():
            raise HTTPException(410, "Download link expired — please request a new export")
        path = Path(job.get("zip_path") or "")
        if not path.is_file():
            raise HTTPException(410, "Export file no longer available")
        # Mark consumed BEFORE we hand out the response. Failed
        # downloads from this point are the client's problem; a
        # security-grade single-use token is more important than a
        # convenient re-download flow.
        await db.privacy_export_jobs.update_one(
            {"_id": job["_id"]},
            {"$set": {"status": "consumed", "token": None, "consumed_at": _now()}},
        )
        return FileResponse(
            path=str(path),
            filename=path.name,
            media_type="application/zip",
        )

    # ── Deletion endpoints ─────────────────────────────────────────────

    @r.get("/delete/status")
    async def deletion_status(u: dict = Depends(get_current_user)):
        """Show whether a pending deletion exists, list blocking orgs,
        and surface the user's transfer-target options."""
        user = await db.users.find_one({"_id": ObjectId(u["id"])})
        pending = user.get("deletion_status") == "pending" if user else False
        requested_at = user.get("deletion_requested_at") if user else None
        scheduled_for = (requested_at + _DELETE_GRACE) if requested_at else None
        blockers = await _find_blocking_owned_orgs(db, u["id"])
        return {
            "pending": pending,
            "requested_at": _iso(requested_at),
            "scheduled_for": _iso(scheduled_for),
            "grace_days": _DELETE_GRACE.days,
            "blockers": blockers,
            "blocked": bool(blockers) and not pending,
        }

    @r.post("/delete")
    async def request_deletion(u: dict = Depends(get_current_user)):
        """Schedule the user for hard deletion after the grace period.
        Refuses if the user is the sole Owner of any active multi-
        member org (caller must transfer first)."""
        blockers = await _find_blocking_owned_orgs(db, u["id"])
        if blockers:
            raise HTTPException(409, {
                "error": "ownership_transfer_required",
                "message": (
                    "You're the sole Owner of one or more orgs with other members. "
                    "Transfer ownership (or remove the other members) before requesting deletion."
                ),
                "blockers": blockers,
            })
        await db.users.update_one(
            {"_id": ObjectId(u["id"])},
            {"$set": {
                "deletion_status": "pending",
                "deletion_requested_at": _now(),
            }},
        )
        return {
            "ok": True,
            "scheduled_for": _iso(_now() + _DELETE_GRACE),
            "grace_days": _DELETE_GRACE.days,
        }

    @r.post("/delete/cancel")
    async def cancel_deletion(u: dict = Depends(get_current_user)):
        """Drop a pending deletion. Idempotent — calling on a not-
        pending account is a no-op."""
        await db.users.update_one(
            {"_id": ObjectId(u["id"])},
            {"$unset": {"deletion_status": "", "deletion_requested_at": ""}},
        )
        return {"ok": True}

    @r.post("/delete/transfer-ownership")
    async def transfer_ownership(
        body: Dict[str, Any] = Body(...),
        u: dict = Depends(get_current_user),
    ):
        """Promote `body.new_owner_id` to Owner of `body.org_id` and
        demote the requester to Admin. The requester must currently
        be Owner of the org and `new_owner_id` must be an existing
        member.
        """
        org_id = (body.get("org_id") or "").strip()
        new_owner = (body.get("new_owner_id") or "").strip()
        if not org_id or not new_owner:
            raise HTTPException(400, "org_id and new_owner_id are required")
        if new_owner == u["id"]:
            raise HTTPException(400, "Cannot transfer ownership to yourself")
        # Verify the requester is currently owner.
        me = await db.org_members.find_one({"org_id": org_id, "user_id": u["id"]})
        if not me or me.get("role") != "owner":
            raise HTTPException(403, "You're not the Owner of this org")
        # Verify the target is a member.
        target = await db.org_members.find_one({"org_id": org_id, "user_id": new_owner})
        if not target:
            raise HTTPException(404, "Target user is not a member of this org")
        # Atomic-ish swap. Mongo doesn't have ACID across two updates
        # without a transaction, but org membership is small enough
        # that a brief inconsistency window is acceptable (an extra
        # Owner for ~1ms). If we needed strict atomicity we'd wrap
        # in a session — overkill here.
        await db.org_members.update_one(
            {"_id": target["_id"]},
            {"$set": {"role": "owner", "promoted_at": _now()}},
        )
        await db.org_members.update_one(
            {"_id": me["_id"]},
            {"$set": {"role": "admin", "demoted_at": _now()}},
        )
        return {"ok": True, "org_id": org_id, "new_owner_id": new_owner}

    return r


# ── Startup helpers (wired from server.py) ─────────────────────────────────


def schedule_privacy_jobs(scheduler, db) -> None:
    """Register daily APScheduler jobs for:
      1. Sweep expired export ZIPs (housekeeping).
      2. Run pending hard deletions whose grace expired.
    Called once at backend startup from server.py.
    """
    async def _sweep_wrapper():
        try:
            await sweep_expired_exports(db)
        except Exception:
            logger.exception("Privacy export sweep failed")

    async def _delete_wrapper():
        try:
            await run_pending_deletions(db)
        except Exception:
            logger.exception("Privacy deletion sweep failed")

    # Both run once a day at 03:30 UTC — off-peak for most operator timezones.
    from apscheduler.triggers.cron import CronTrigger
    scheduler.add_job(_sweep_wrapper, CronTrigger(hour=3, minute=30, timezone="UTC"),
                      id="privacy_export_sweep", replace_existing=True)
    scheduler.add_job(_delete_wrapper, CronTrigger(hour=3, minute=45, timezone="UTC"),
                      id="privacy_delete_sweep", replace_existing=True)
