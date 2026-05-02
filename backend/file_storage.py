"""file_storage.py — MongoDB GridFS-backed persistent file storage.

Why this exists:
    Production deploys on Emergent run in containers whose local filesystem
    (/app/backend/uploads/) does NOT persist across redeploys. Mission
    artifacts, generated images, brand logos, and chat attachments written
    to that path silently vanished every release, leaving users with
    `{"detail":"File missing on disk"}` errors when they tried to download
    earlier work.

    GridFS lives inside the same MongoDB cluster the rest of the app already
    persists to, so anything written via these helpers survives every
    redeploy with zero infrastructure setup.

How callers use it:
    - On WRITE: keep doing whatever you were doing to disk (so the existing
      serve paths still work in dev / preview), AND call
      ``save_blob_to_gridfs`` to mirror the bytes into Mongo. Persist the
      returned ``str(file_id)`` next to the on-disk filename in the row.
    - On READ: try the disk read first (fast, free). If the file isn't
      there but the row has a ``gridfs_id`` attached, call
      ``read_blob_from_gridfs`` to recover the bytes.

Two collections are used by GridFS automatically:
    - ``fs.files`` — metadata (filename, mime, length, uploadDate)
    - ``fs.chunks`` — 255 KB chunks of binary data
"""
from __future__ import annotations

import logging
from typing import Optional

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorGridFSBucket, AsyncIOMotorDatabase

logger = logging.getLogger("file_storage")


def get_bucket(db: AsyncIOMotorDatabase) -> AsyncIOMotorGridFSBucket:
    """Build a per-call GridFSBucket; cheap, no need to memoise."""
    return AsyncIOMotorGridFSBucket(db)


async def save_blob_to_gridfs(
    db: AsyncIOMotorDatabase,
    content: bytes,
    filename: str,
    mime: Optional[str] = None,
) -> Optional[str]:
    """Persist ``content`` to GridFS and return ``str(file_id)``.

    Returns ``None`` on failure so the caller can keep going with disk-only
    storage instead of crashing the request. We log the exception so issues
    surface in supervisor logs but don't break user flows.
    """
    try:
        bucket = get_bucket(db)
        metadata = {"mime": mime} if mime else None
        file_id = await bucket.upload_from_stream(
            filename,
            content,
            metadata=metadata,
        )
        return str(file_id)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("GridFS write failed for %s: %s", filename, exc)
        return None


async def read_blob_from_gridfs(
    db: AsyncIOMotorDatabase,
    file_id: str,
) -> Optional[bytes]:
    """Return raw bytes for a previously-saved GridFS file, or ``None``
    if the id is invalid / missing."""
    try:
        oid = ObjectId(file_id)
    except Exception:
        return None
    try:
        bucket = get_bucket(db)
        stream = await bucket.open_download_stream(oid)
        return await stream.read()
    except Exception as exc:
        logger.warning("GridFS read failed for %s: %s", file_id, exc)
        return None


async def delete_blob_from_gridfs(
    db: AsyncIOMotorDatabase,
    file_id: str,
) -> None:
    """Best-effort delete; never raises."""
    try:
        oid = ObjectId(file_id)
    except Exception:
        return
    try:
        bucket = get_bucket(db)
        await bucket.delete(oid)
    except Exception:
        pass
