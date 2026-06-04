"""org_cleanup.py — comprehensive cascade-purge for an organization.

Before Feb 2026, two separate code paths half-deleted an org's data:

  • ``DELETE /api/orgs/{id}`` (server.py) cleaned only: teams,
    conversations, messages, invites, org_members, assets, schedules.
    It left behind documents, missions, audits, social_posts, brand
    logos, composio_connections (with live OAuth tokens!), and every
    underlying file on disk + GridFS.

  • ``privacy._hard_delete_user`` cleaned slightly more for SOLO-owned
    orgs (documents/missions/audits/social_posts metadata rows) but
    STILL left the disk + GridFS bytes orphaned.

This module is the single source of truth. ``purge_org_data`` walks
every org-scoped collection, collects file references, deletes the
underlying bytes from disk AND GridFS, drops the metadata rows, and
returns a stats dict for audit logging.

It's deliberately conservative about Stripe / payment / audit-log
data — those are documented retained-by-policy (tax compliance + 12mo
security investigation window) and live on the USER object, not the
org, so they're out of scope for this module.

Public surface:
    ``purge_org_data(db, org_id, *, upload_dir, assets_dir, docs_dir,
                     conv_upload_dir, revoke_composio=True) -> dict``

Callers MUST have already authorised the action (delete_org checks
owner role; privacy hard-delete vetoes via the wizard).
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from bson import ObjectId

from file_storage import delete_blob_from_gridfs

logger = logging.getLogger("foundry.org_cleanup")


# ── Filename collectors ──────────────────────────────────────────────────────
# Each collector yields the on-disk basenames + any explicit gridfs_ids
# referenced by an org's rows. Designed to fail-soft — a missing field
# or unexpected shape just yields nothing rather than throwing.


async def _filenames_from_social_posts(db, org_id: str) -> List[str]:
    """Cover image, raw composited image, video, music-video filenames."""
    out: List[str] = []
    cur = db.social_posts.find(
        {"org_id": org_id},
        {"_id": 0, "image": 1, "raw_image": 1, "video": 1, "music_video": 1},
    )
    async for p in cur:
        for key in ("image", "raw_image", "video", "music_video"):
            blob = p.get(key) or {}
            fn = blob.get("filename") if isinstance(blob, dict) else None
            if isinstance(fn, str) and fn:
                out.append(fn)
    return out


async def _docs_metadata(db, org_id: str) -> List[Dict[str, Any]]:
    """Documents library — both on-disk filename AND explicit gridfs_id."""
    cur = db.documents.find(
        {"org_id": org_id},
        {"_id": 0, "filename": 1, "gridfs_id": 1},
    )
    return [d async for d in cur]


async def _filenames_from_assets(db, org_id: str) -> List[str]:
    """Asset library — single `filename` field per row."""
    cur = db.assets.find(
        {"org_id": org_id},
        {"_id": 0, "filename": 1},
    )
    return [d.get("filename") async for d in cur if d.get("filename")]


async def _filenames_from_audits(db, org_id: str) -> List[str]:
    """Audits + business_audits PDFs."""
    out: List[str] = []
    for coll in ("audits", "business_audits"):
        try:
            cur = db[coll].find(
                {"org_id": org_id},
                {"_id": 0, "pdf_filename": 1, "report_filename": 1},
            )
            async for d in cur:
                for k in ("pdf_filename", "report_filename"):
                    v = d.get(k)
                    if isinstance(v, str) and v:
                        out.append(v)
        except Exception:
            pass
    return out


async def _filenames_from_brand(db, org_id: str) -> List[str]:
    """Brand primary + alt logo filenames live on the organization row."""
    try:
        org = await db.organizations.find_one(
            {"_id": ObjectId(org_id)},
            {"_id": 0, "brand": 1},
        )
    except Exception:
        return []
    brand = (org or {}).get("brand") or {}
    out: List[str] = []
    for key in ("primary_logo", "alt_logo"):
        b = brand.get(key) or {}
        if isinstance(b, dict):
            fn = b.get("filename")
            if isinstance(fn, str) and fn:
                out.append(fn)
    return out


async def _filenames_from_missions(db, org_id: str) -> List[str]:
    """Mission artifacts can carry their own filename strings + attachments."""
    out: List[str] = []
    cur = db.missions.find(
        {"org_id": org_id},
        {"_id": 0, "artifacts": 1, "attachments": 1},
    )
    async for m in cur:
        for art in (m.get("artifacts") or []):
            if isinstance(art, dict):
                fn = art.get("filename")
                if isinstance(fn, str) and fn:
                    out.append(fn)
        for att in (m.get("attachments") or []):
            if isinstance(att, dict):
                fn = att.get("filename")
                if isinstance(fn, str) and fn:
                    out.append(fn)
    return out


async def _filenames_from_mission_attachments(db, org_id: str) -> List[str]:
    """Separate collection (used for some mission-input uploads)."""
    try:
        cur = db.mission_attachments.find(
            {"org_id": org_id},
            {"_id": 0, "filename": 1},
        )
        return [d.get("filename") async for d in cur if d.get("filename")]
    except Exception:
        return []


# ── GridFS + disk byte cleanup ───────────────────────────────────────────────


async def _delete_gridfs_for_filenames(db, filenames: List[str]) -> int:
    """For each filename, look up its ``media_files`` row → gridfs_id →
    delete the blob. Returns count successfully deleted. Best-effort —
    missing rows are silent (they were never persisted to GridFS).

    Also deletes the ``media_files`` index row itself so subsequent
    lookups don't dangle.
    """
    if not filenames:
        return 0
    unique = sorted({fn for fn in filenames if isinstance(fn, str) and fn})
    if not unique:
        return 0
    deleted = 0
    rows = []
    try:
        cur = db.media_files.find(
            {"filename": {"$in": unique}},
            {"_id": 0, "filename": 1, "gridfs_id": 1},
        )
        rows = [r async for r in cur]
    except Exception as exc:
        logger.warning("media_files scan failed: %s", exc)
    for r in rows:
        gid = r.get("gridfs_id")
        if gid:
            try:
                await delete_blob_from_gridfs(db, gid)
                deleted += 1
            except Exception as exc:
                logger.warning("GridFS delete failed for %s: %s", gid, exc)
    try:
        await db.media_files.delete_many({"filename": {"$in": unique}})
    except Exception as exc:
        logger.warning("media_files row delete failed: %s", exc)
    return deleted


async def _delete_explicit_gridfs_ids(db, gridfs_ids: List[str]) -> int:
    """For collections (like ``documents``) that store the gridfs_id
    directly on the row, delete those blobs too. Idempotent with the
    filename-based path above — GridFS delete is best-effort and never
    raises on missing ids."""
    if not gridfs_ids:
        return 0
    n = 0
    for gid in gridfs_ids:
        if not gid:
            continue
        try:
            await delete_blob_from_gridfs(db, gid)
            n += 1
        except Exception:
            pass
    return n


def _delete_disk_files(filenames: List[str], dirs: List[Path]) -> int:
    """Walk each directory, unlink any basename in ``filenames``.
    Returns count of files successfully unlinked. Files we don't own
    or that already vanished are silently skipped."""
    if not filenames or not dirs:
        return 0
    targets = {fn for fn in filenames if isinstance(fn, str) and fn}
    if not targets:
        return 0
    n = 0
    for root in dirs:
        if not root or not root.exists():
            continue
        for fn in targets:
            p = root / fn
            try:
                if p.is_file():
                    p.unlink(missing_ok=True)
                    n += 1
            except Exception:
                pass
    return n


# ── Composio revocation ──────────────────────────────────────────────────────


async def _revoke_composio_for_org(db, org_id: str) -> int:
    """Best-effort revoke every Composio connection attached to this org
    via the Composio API, then drop our local rows. Returns the number
    of rows we successfully removed.

    Failure to call Composio (network, missing API key, expired creds)
    is logged but never blocks the local row deletion — leaving orphan
    rows in our DB pointing at upstream-revoked accounts is worse than
    leaving upstream accounts dangling for a manual sweep."""
    try:
        from composio_integration import is_available, get_client
    except Exception as exc:
        logger.warning("Composio module missing during purge: %s", exc)
        try:
            res = await db.composio_connections.delete_many({"org_id": org_id})
            return res.deleted_count
        except Exception:
            return 0
    rows_cur = db.composio_connections.find(
        {"org_id": org_id},
        {"_id": 0, "connection_id": 1},
    )
    rows = [r async for r in rows_cur]
    if not rows:
        return 0
    if is_available():
        try:
            client = get_client()
        except Exception as exc:
            logger.warning("Composio client unavailable during purge: %s", exc)
            client = None
        if client is not None:
            for r in rows:
                cid = r.get("connection_id")
                if not cid:
                    continue
                try:
                    # Composio v3 delete is sync; we call it inline.
                    client.connected_accounts.delete(cid)
                except Exception as exc:
                    logger.warning(
                        "Composio revoke failed for %s: %s", cid, exc,
                    )
    try:
        res = await db.composio_connections.delete_many({"org_id": org_id})
        return res.deleted_count
    except Exception:
        return 0


# ── Conversation-attachment dir ─────────────────────────────────────────────


def _delete_conv_dirs(conv_ids: List[str], conv_upload_dir: Optional[Path]) -> int:
    """Each conversation may own a subfolder of CONV_UPLOAD_DIR holding
    user-uploaded attachments. Walk and rmtree them. Returns count
    removed."""
    if not conv_ids or not conv_upload_dir or not conv_upload_dir.exists():
        return 0
    n = 0
    for cid in conv_ids:
        if not cid:
            continue
        sub = conv_upload_dir / cid
        if sub.exists() and sub.is_dir():
            try:
                shutil.rmtree(sub, ignore_errors=True)
                n += 1
            except Exception:
                pass
    return n


# ── Public entrypoint ────────────────────────────────────────────────────────


async def purge_org_data(
    db,
    org_id: str,
    *,
    upload_dir: Optional[Path] = None,
    assets_dir: Optional[Path] = None,
    docs_dir: Optional[Path] = None,
    conv_upload_dir: Optional[Path] = None,
    revoke_composio: bool = True,
    delete_organization_row: bool = True,
) -> Dict[str, int]:
    """Full cascade purge for an organization.

    Returns a stats dict like::

        {"social_posts": 14, "documents": 22, "assets": 8, ...,
         "gridfs_blobs": 38, "disk_files": 41, "composio_rows": 3}

    The dict shape is suitable for logging to an audit collection so
    support can later answer "what did we delete for this org?".
    """
    stats: Dict[str, int] = {}

    # ── 1. Collect filenames + explicit gridfs_ids BEFORE any delete. ──
    filenames: List[str] = []
    explicit_gridfs: List[str] = []

    filenames.extend(await _filenames_from_social_posts(db, org_id))
    docs_meta = await _docs_metadata(db, org_id)
    for d in docs_meta:
        fn = d.get("filename")
        if isinstance(fn, str) and fn:
            filenames.append(fn)
        gid = d.get("gridfs_id")
        if isinstance(gid, str) and gid:
            explicit_gridfs.append(gid)
    filenames.extend(await _filenames_from_assets(db, org_id))
    filenames.extend(await _filenames_from_audits(db, org_id))
    filenames.extend(await _filenames_from_brand(db, org_id))
    filenames.extend(await _filenames_from_missions(db, org_id))
    filenames.extend(await _filenames_from_mission_attachments(db, org_id))

    stats["files_referenced"] = len({f for f in filenames if f})

    # ── 2. Conversation ids (we need them to cascade messages + dirs). ──
    teams_cur = db.teams.find({"org_id": org_id}, {"_id": 1})
    team_ids = [str(t["_id"]) async for t in teams_cur]
    conv_ids: List[str] = []
    if team_ids:
        convs_cur = db.conversations.find(
            {"team_id": {"$in": team_ids}}, {"_id": 1, "id": 1},
        )
        async for c in convs_cur:
            conv_ids.append(c.get("id") or str(c.get("_id")))

    # ── 3. Delete the actual bytes (GridFS + disk). ───────────────────
    stats["gridfs_blobs"] = (
        await _delete_gridfs_for_filenames(db, filenames)
        + await _delete_explicit_gridfs_ids(db, explicit_gridfs)
    )
    stats["disk_files"] = _delete_disk_files(
        filenames,
        [d for d in (upload_dir, assets_dir, docs_dir) if d],
    )
    stats["conv_dirs"] = _delete_conv_dirs(conv_ids, conv_upload_dir)

    # ── 4. Composio revoke + drop our rows. ───────────────────────────
    if revoke_composio:
        stats["composio_rows"] = await _revoke_composio_for_org(db, org_id)
    else:
        try:
            res = await db.composio_connections.delete_many({"org_id": org_id})
            stats["composio_rows"] = res.deleted_count
        except Exception:
            stats["composio_rows"] = 0

    # ── 5. Scheduler tear-down (run BEFORE we delete the schedule rows
    #       so the in-process APScheduler unregisters cleanly). The
    #       caller is responsible for unregistering — we collect ids
    #       and surface them so server.py can call _scheduler_unregister
    #       in its own context (the scheduler isn't import-safe here).
    sched_ids: List[str] = []
    try:
        sched_cur = db.schedules.find({"org_id": org_id}, {"_id": 1})
        sched_ids = [str(s["_id"]) async for s in sched_cur]
    except Exception:
        sched_ids = []
    stats["schedule_ids"] = len(sched_ids)

    # ── 6. Delete every org-scoped metadata collection. Each line
    #       captures one cleanly-modeled "org_id" collection. New
    #       org-scoped collections added later MUST add a delete here. ─
    collection_targets = [
        # Already in old delete_org (kept):
        "invites", "org_members", "assets", "schedules",
        # Newly added (the gap from the prior implementation):
        "documents", "missions", "audits", "business_audits",
        "social_posts", "social_sessions", "org_notes", "soul_suggestions",
        "mission_events", "mission_attachments", "mission_browser_states",
        "agent_tool_prefs", "custom_agents", "conversation_summaries",
        "pulse_nudges", "billing_overage_invoices", "audit_leads",
        "domain_credentials", "github_connections", "integrity_proofs",
        "trial_counters", "composio_auth_configs", "telegram_disambig_state",
        "telegram_inbound_log",
        # Cost / telemetry tagged with org_id (keep aggregate analytics
        # docs say "no PII" — these rows include org_id which is PII-
        # adjacent, so we drop them on org delete). Stripe billing rows
        # (`payment_transactions`) we DO retain for tax-compliance.
        "external_api_calls", "llm_calls",
    ]
    for coll in collection_targets:
        try:
            res = await db[coll].delete_many({"org_id": org_id})
            if res.deleted_count:
                stats[coll] = res.deleted_count
        except Exception as exc:
            logger.warning("Purge of %s for org %s failed: %s", coll, org_id, exc)

    # ── 7. Messages + conversations + teams (depend on conv_ids). ─────
    if conv_ids:
        try:
            r = await db.messages.delete_many({"conversation_id": {"$in": conv_ids}})
            stats["messages"] = r.deleted_count
        except Exception as exc:
            logger.warning("messages cascade failed: %s", exc)
        try:
            r = await db.conversations.delete_many({
                "_id": {"$in": [ObjectId(c) for c in conv_ids if ObjectId.is_valid(c)]},
            })
            if r.deleted_count:
                stats["conversations"] = stats.get("conversations", 0) + r.deleted_count
        except Exception:
            pass
        # Some convs are keyed by string `id`, not ObjectId
        try:
            r = await db.conversations.delete_many({"id": {"$in": conv_ids}})
            if r.deleted_count:
                stats["conversations"] = stats.get("conversations", 0) + r.deleted_count
        except Exception:
            pass
    if team_ids:
        try:
            r = await db.teams.delete_many({
                "_id": {"$in": [ObjectId(t) for t in team_ids if ObjectId.is_valid(t)]},
            })
            stats["teams"] = r.deleted_count
        except Exception:
            pass

    # ── 8. The organization row itself. ──────────────────────────────
    if delete_organization_row:
        try:
            await db.organizations.delete_one({"_id": ObjectId(org_id)})
            stats["organization"] = 1
        except Exception as exc:
            logger.warning("organization row delete failed: %s", exc)
            stats["organization"] = 0

    # ── 9. Audit log of the purge itself. ────────────────────────────
    try:
        from datetime import datetime, timezone
        await db.admin_access_grants.insert_one({
            "kind": "org_purge",
            "org_id": org_id,
            "stats": stats,
            "schedule_ids": sched_ids,  # for caller-side scheduler teardown
            "at": datetime.now(timezone.utc),
        })
    except Exception:
        pass

    # Caller MUST call ``_scheduler_unregister`` for each id in
    # ``stats.pop("schedule_ids", [])`` — we can't import the scheduler
    # safely from this module without circular deps. We stash the ids
    # in stats so the caller has them readily available.
    stats["_schedule_ids"] = sched_ids  # type: ignore[assignment]

    return stats
