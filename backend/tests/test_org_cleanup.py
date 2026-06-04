"""End-to-end tests for the centralised org cascade-purge module.

We use a REAL MongoDB instance (the one MONGO_URL points at) with a
unique-per-test DB name. GridFS lives inside the same cluster, so
this is the only way to exercise the actual ``delete_blob_from_gridfs``
path — mocking it would just verify our mocks.

The tests cover:
  • Every org-scoped collection touched by the purge is empty afterwards.
  • Documents on disk (``UPLOAD_DIR`` + ``ASSETS_DIR`` + ``DOCS_DIR``)
    referenced by purged rows are unlinked.
  • GridFS blobs referenced by purged rows are gone.
  • Composio rows are dropped (revoke API is mocked — we just verify
    the local row deletion).
  • Per-conversation upload subdirectories are recursively removed.
  • ``stats`` dict reports accurate counts the caller can audit-log.
  • Sibling orgs the user is also a member of are LEFT INTACT
    (purge is strictly per-org).
"""
from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

import org_cleanup
from file_storage import save_blob_to_gridfs, read_blob_from_gridfs


@pytest_asyncio.fixture
async def db():
    """Real MongoDB connection on a throwaway DB. Drops on teardown."""
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        pytest.skip("MONGO_URL not configured in this environment.")
    name = f"orgcleanup_{uuid.uuid4().hex[:10]}"
    client = AsyncIOMotorClient(mongo_url)
    db = client[name]
    yield db
    client.drop_database(name)
    client.close()


@pytest.fixture
def tmpdirs(tmp_path: Path):
    """Stand-in for UPLOAD_DIR / ASSETS_DIR / DOCS_DIR / CONV_UPLOAD_DIR."""
    upload = tmp_path / "uploads"
    assets = upload / "assets"
    docs = upload / "docs"
    conv = upload / "conv"
    for p in (upload, assets, docs, conv):
        p.mkdir(parents=True, exist_ok=True)
    return {"upload": upload, "assets": assets, "docs": docs, "conv": conv}


async def _seed_full_org(db, tmpdirs) -> dict:
    """Populate one org with every kind of cascade-relevant data so
    we can prove the purge nukes all of it. Returns a dict of
    identifiers used by assertions."""
    org_oid = ObjectId()
    org_id = str(org_oid)
    other_org_id = str(ObjectId())  # sibling — must be untouched

    # Organization rows.
    await db.organizations.insert_many([
        {"_id": org_oid, "name": "Doomed Co",
         "brand": {
             "primary_logo": {"filename": "logo-primary.png"},
             "alt_logo": {"filename": "logo-alt.png"},
         }},
        {"_id": ObjectId(other_org_id), "name": "Sibling Co"},
    ])

    # Teams + conversations + messages.
    team_oid = ObjectId()
    other_team_oid = ObjectId()
    await db.teams.insert_many([
        {"_id": team_oid, "org_id": org_id, "name": "Core"},
        {"_id": other_team_oid, "org_id": other_org_id, "name": "Sibling Core"},
    ])
    conv_oid_in_org = ObjectId()
    conv_oid_other = ObjectId()
    await db.conversations.insert_many([
        {"_id": conv_oid_in_org, "team_id": str(team_oid),
         "id": str(conv_oid_in_org), "title": "Doomed chat"},
        {"_id": conv_oid_other, "team_id": str(other_team_oid),
         "id": str(conv_oid_other), "title": "Sibling chat"},
    ])
    await db.messages.insert_many([
        {"conversation_id": str(conv_oid_in_org), "text": "hi"},
        {"conversation_id": str(conv_oid_in_org), "text": "bye"},
        {"conversation_id": str(conv_oid_other), "text": "untouched"},
    ])

    # Org-scoped collections.
    for coll in [
        "documents", "missions", "audits", "business_audits",
        "social_posts", "social_sessions", "org_notes", "soul_suggestions",
        "mission_events", "mission_attachments", "agent_tool_prefs",
        "custom_agents", "conversation_summaries", "pulse_nudges",
        "billing_overage_invoices", "audit_leads", "domain_credentials",
        "external_api_calls", "llm_calls",
    ]:
        await db[coll].insert_one({"org_id": org_id, "_sentinel": True})
        await db[coll].insert_one({"org_id": other_org_id, "_sentinel": True})

    await db.assets.insert_many([
        {"org_id": org_id, "filename": "asset.png"},
        {"org_id": other_org_id, "filename": "sibling-asset.png"},
    ])
    await db.invites.insert_one({"org_id": org_id, "email": "x@y.com"})
    await db.org_members.insert_many([
        {"org_id": org_id, "user_id": "u1", "role": "owner"},
        {"org_id": org_id, "user_id": "u2", "role": "member"},
        {"org_id": other_org_id, "user_id": "u1", "role": "owner"},
    ])
    await db.schedules.insert_one({"org_id": org_id, "rule": "@daily"})

    # ── On-disk files + GridFS blobs ─────────────────────────────
    # Each filename gets a real on-disk file AND a GridFS mirror so we
    # can verify both layers are cleaned.
    filenames_on_disk = {
        tmpdirs["upload"]: ["logo-primary.png", "logo-alt.png",
                            "cover.png", "raw.png", "clip.mp4", "mv.mp4",
                            "audit.pdf"],
        tmpdirs["assets"]: ["asset.png"],
        tmpdirs["docs"]: ["doc1.pdf"],
    }
    for parent, names in filenames_on_disk.items():
        for fn in names:
            (parent / fn).write_bytes(b"binary-payload-" + fn.encode())

    # GridFS mirrors via the same helper production code uses.
    gridfs_ids = {}
    for parent, names in filenames_on_disk.items():
        for fn in names:
            gid = await save_blob_to_gridfs(
                db, (parent / fn).read_bytes(), fn, mime="application/octet-stream",
            )
            gridfs_ids[fn] = gid
            # media_files row mirrors what persist_bytes() writes.
            await db.media_files.update_one(
                {"filename": fn},
                {"$set": {"filename": fn, "gridfs_id": gid,
                          "mime": "application/octet-stream",
                          "created_at": datetime.now(timezone.utc)}},
                upsert=True,
            )

    # Now wire the doc / asset / brand / social rows to those filenames.
    explicit_doc_gid = await save_blob_to_gridfs(
        db, b"doc-blob", "doc1.pdf", mime="application/pdf",
    )
    await db.documents.delete_many({"org_id": org_id})
    await db.documents.insert_one({
        "org_id": org_id, "filename": "doc1.pdf",
        "gridfs_id": explicit_doc_gid,
    })
    await db.documents.insert_one({"org_id": other_org_id, "filename": "untouched.pdf"})
    # Override sentinel social_post with one carrying file refs.
    await db.social_posts.delete_many({"org_id": org_id})
    await db.social_posts.insert_one({
        "org_id": org_id,
        "image": {"filename": "cover.png"},
        "raw_image": {"filename": "raw.png"},
        "video": {"filename": "clip.mp4"},
        "music_video": {"filename": "mv.mp4"},
    })
    await db.audits.delete_many({"org_id": org_id})
    await db.audits.insert_one({"org_id": org_id, "pdf_filename": "audit.pdf"})

    # Composio rows — org-scoped (purge revokes these).
    await db.composio_connections.insert_many([
        {"org_id": org_id, "connection_id": "conn_a",
         "composio_user_id": f"{org_id}-u1", "status": "ACTIVE"},
        {"org_id": other_org_id, "connection_id": "conn_b",
         "composio_user_id": f"{other_org_id}-u1", "status": "ACTIVE"},
    ])

    # Per-conversation upload subfolders.
    (tmpdirs["conv"] / str(conv_oid_in_org)).mkdir(parents=True, exist_ok=True)
    (tmpdirs["conv"] / str(conv_oid_in_org) / "upload.png").write_bytes(b"x")
    (tmpdirs["conv"] / str(conv_oid_other)).mkdir(parents=True, exist_ok=True)
    (tmpdirs["conv"] / str(conv_oid_other) / "keep.png").write_bytes(b"y")

    return {
        "org_id": org_id, "other_org_id": other_org_id,
        "conv_id_in_org": str(conv_oid_in_org),
        "conv_id_other": str(conv_oid_other),
        "gridfs_ids": gridfs_ids,
        "explicit_doc_gid": explicit_doc_gid,
    }


# ── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_purge_drops_every_org_scoped_collection(db, tmpdirs):
    info = await _seed_full_org(db, tmpdirs)

    stats = await org_cleanup.purge_org_data(
        db, info["org_id"],
        upload_dir=tmpdirs["upload"],
        assets_dir=tmpdirs["assets"],
        docs_dir=tmpdirs["docs"],
        conv_upload_dir=tmpdirs["conv"],
        revoke_composio=False,  # avoid real Composio client call
    )

    # No org-scoped rows left for the purged org.
    for coll in [
        "documents", "missions", "audits", "business_audits",
        "social_posts", "social_sessions", "org_notes", "soul_suggestions",
        "mission_events", "mission_attachments", "agent_tool_prefs",
        "custom_agents", "conversation_summaries", "pulse_nudges",
        "billing_overage_invoices", "audit_leads", "domain_credentials",
        "external_api_calls", "llm_calls", "assets", "invites",
        "org_members", "schedules", "composio_connections", "teams",
    ]:
        n = await db[coll].count_documents({"org_id": info["org_id"]})
        assert n == 0, f"{coll} still has {n} rows for purged org"

    # Sibling org untouched.
    for coll in ["documents", "social_posts", "audits", "composio_connections",
                  "assets", "org_members", "teams"]:
        n = await db[coll].count_documents({"org_id": info["other_org_id"]})
        assert n >= 1, f"sibling {coll} got nuked!"

    # Organization row gone; sibling preserved.
    purged = await db.organizations.find_one({"_id": ObjectId(info["org_id"])})
    sibling = await db.organizations.find_one({"_id": ObjectId(info["other_org_id"])})
    assert purged is None
    assert sibling is not None
    assert stats.get("organization") == 1


@pytest.mark.asyncio
async def test_purge_deletes_gridfs_blobs_and_disk_files(db, tmpdirs):
    info = await _seed_full_org(db, tmpdirs)
    purged_files = ["logo-primary.png", "logo-alt.png", "cover.png",
                    "raw.png", "clip.mp4", "mv.mp4", "audit.pdf",
                    "asset.png", "doc1.pdf"]
    sibling_file = (tmpdirs["upload"] / "should-not-exist.txt")  # control
    sibling_file.write_bytes(b"keep-me")

    await org_cleanup.purge_org_data(
        db, info["org_id"],
        upload_dir=tmpdirs["upload"],
        assets_dir=tmpdirs["assets"],
        docs_dir=tmpdirs["docs"],
        conv_upload_dir=tmpdirs["conv"],
        revoke_composio=False,
    )

    # On-disk bytes gone for purged refs.
    for fn in purged_files:
        for root in (tmpdirs["upload"], tmpdirs["assets"], tmpdirs["docs"]):
            assert not (root / fn).exists(), f"{fn} still on disk under {root}"
    # GridFS blobs for those filenames gone too.
    for fn in purged_files:
        gid = info["gridfs_ids"].get(fn)
        if gid:
            blob = await read_blob_from_gridfs(db, gid)
            assert blob is None, f"GridFS blob for {fn} ({gid}) was not deleted"
    # Explicit doc gridfs_id (stored directly on the documents row).
    blob = await read_blob_from_gridfs(db, info["explicit_doc_gid"])
    assert blob is None
    # Sentinel sibling file untouched.
    assert sibling_file.exists()
    # media_files index rows for purged filenames gone.
    n = await db.media_files.count_documents({"filename": {"$in": purged_files}})
    assert n == 0


@pytest.mark.asyncio
async def test_purge_cleans_per_conversation_upload_dirs(db, tmpdirs):
    info = await _seed_full_org(db, tmpdirs)
    await org_cleanup.purge_org_data(
        db, info["org_id"],
        upload_dir=tmpdirs["upload"],
        assets_dir=tmpdirs["assets"],
        docs_dir=tmpdirs["docs"],
        conv_upload_dir=tmpdirs["conv"],
        revoke_composio=False,
    )
    # Conv subfolder for this org's conv gone, sibling org's conv preserved.
    assert not (tmpdirs["conv"] / info["conv_id_in_org"]).exists()
    assert (tmpdirs["conv"] / info["conv_id_other"]).exists()


@pytest.mark.asyncio
async def test_purge_revoke_composio_false_still_drops_local_rows(db, tmpdirs):
    info = await _seed_full_org(db, tmpdirs)
    await org_cleanup.purge_org_data(
        db, info["org_id"],
        upload_dir=tmpdirs["upload"],
        assets_dir=tmpdirs["assets"],
        docs_dir=tmpdirs["docs"],
        conv_upload_dir=tmpdirs["conv"],
        revoke_composio=False,
    )
    n_purged = await db.composio_connections.count_documents(
        {"org_id": info["org_id"]},
    )
    n_sibling = await db.composio_connections.count_documents(
        {"org_id": info["other_org_id"]},
    )
    assert n_purged == 0
    assert n_sibling == 1


@pytest.mark.asyncio
async def test_purge_stats_contains_actionable_counts(db, tmpdirs):
    info = await _seed_full_org(db, tmpdirs)
    stats = await org_cleanup.purge_org_data(
        db, info["org_id"],
        upload_dir=tmpdirs["upload"],
        assets_dir=tmpdirs["assets"],
        docs_dir=tmpdirs["docs"],
        conv_upload_dir=tmpdirs["conv"],
        revoke_composio=False,
    )
    # Sanity: every key our caller will log.
    assert stats.get("organization") == 1
    assert stats.get("disk_files", 0) >= 7
    assert stats.get("gridfs_blobs", 0) >= 7
    assert stats.get("conv_dirs", 0) >= 1
    assert stats.get("composio_rows", 0) >= 1
    assert stats.get("messages", 0) >= 2
    assert stats.get("teams", 0) >= 1
    assert stats.get("documents", 0) >= 1
    assert "_schedule_ids" in stats  # caller uses this to unregister
