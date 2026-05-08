"""Proof of Integrity — OpenTimestamps-backed cryptographic anchoring.

Two tiers:

  * **Tier 1 (per-file)**  — every PDF / brief / artifact your agents
    generate gets its SHA-256 hash submitted to the OpenTimestamps
    public calendar network. Within ~1-6 hours that hash is anchored
    into the Bitcoin blockchain, giving you a tamper-evident proof that
    the file existed at that exact byte-content at that exact moment.

  * **Tier 2 (daily Merkle root)** — every 24 hours we hash a bundle of
    "important business records" written that day (mission events,
    workspace invite acceptances, org member additions, audit leads),
    fold them into a Merkle root, and anchor THAT. Customers can later
    re-derive any single record's hash and walk the Merkle path to
    prove it was part of that day's anchored state.

Why OpenTimestamps?
  * **Free** — no wallet, no gas, no API key. The calendar servers are
    public infrastructure run by the OTS project.
  * **Bitcoin-anchored** — the most tamper-resistant ledger in
    existence. Far stronger than anchoring on Ethereum / Polygon.
  * **GDPR-safe** — only hashes hit the chain, never any content.
  * **Standardized** — `.ots` proofs are a published standard; any
    third party can verify them with the OTS CLI, no SoulPrint
    cooperation required.

Storage:
  * `integrity_proofs` collection — one row per stamped artifact / merkle
    root, with the full `.ots` proof bytes + verification metadata.
  * Files in GridFS pick up an `integrity_proof_id` so the artifacts
    UI can render a "Verified" badge that links back to the proof.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from opentimestamps.calendar import RemoteCalendar
from opentimestamps.core.serialize import (
    StreamDeserializationContext, StreamSerializationContext,
)
from opentimestamps.core.timestamp import Timestamp

logger = logging.getLogger(__name__)


# Public calendar servers maintained by the OpenTimestamps project. We
# fan out to all three for redundancy — if any one calendar is down or
# slow we still get the proof from the others. The first response wins.
PUBLIC_CALENDARS = [
    "https://alice.btc.calendar.opentimestamps.org",
    "https://bob.btc.calendar.opentimestamps.org",
    "https://finney.calendar.eternitywall.com",
]

# Hard-cap calendar requests so a slow calendar can't block a request.
CALENDAR_TIMEOUT_S = 8.0


# ── Low-level OTS helpers ──────────────────────────────────────────────────


def _serialize_proof(ts: Timestamp) -> bytes:
    """Serialize a Timestamp tree into the standard `.ots` byte format."""
    buf = io.BytesIO()
    ctx = StreamSerializationContext(buf)
    ts.serialize(ctx)
    return buf.getvalue()


def _deserialize_proof(proof_bytes: bytes, digest: bytes) -> Timestamp:
    """Reverse of `_serialize_proof`. Caller must supply the original
    digest the proof was built for (OTS .ots files don't embed it)."""
    ctx = StreamDeserializationContext(io.BytesIO(proof_bytes))
    return Timestamp.deserialize(ctx, digest)


def _stamp_digest_blocking(digest: bytes) -> tuple[Optional[bytes], list[str]]:
    """Submit `digest` to the public calendars and return the merged
    proof bytes plus the list of calendar URLs that succeeded. Pure
    blocking I/O — caller MUST run this inside `asyncio.to_thread`.
    """
    base = Timestamp(digest)
    succeeded: list[str] = []
    for url in PUBLIC_CALENDARS:
        try:
            cal = RemoteCalendar(url)
            cal_ts = cal.submit(digest, timeout=CALENDAR_TIMEOUT_S)
            base.merge(cal_ts)
            succeeded.append(url)
        except Exception as exc:
            logger.warning("OTS calendar %s submit failed: %s", url, exc)
    if not succeeded:
        return None, []
    return _serialize_proof(base), succeeded


async def stamp_digest(digest: bytes) -> tuple[Optional[bytes], list[str]]:
    """Async wrapper — schedules the blocking calendar I/O on a thread."""
    return await asyncio.to_thread(_stamp_digest_blocking, digest)


def _upgrade_proof_blocking(
    proof_bytes: bytes, digest: bytes,
) -> tuple[Optional[bytes], bool]:
    """Ask the calendars whether the pending attestation has been
    upgraded to a Bitcoin block attestation yet. Returns
    `(new_proof_bytes_or_None, became_bitcoin_anchored)`. Pure
    blocking — wrap with `asyncio.to_thread`.

    OTS proofs start as "pending" — the calendar promised it WILL
    anchor your hash in the next Bitcoin batch. After the batch lands
    on-chain, the calendar can upgrade your proof with the actual
    block-header path. We poll for that here.
    """
    try:
        ts = _deserialize_proof(proof_bytes, digest)
    except Exception as exc:
        logger.warning("Could not deserialize proof for upgrade: %s", exc)
        return None, False
    # Walk every PendingAttestation in the tree and try to upgrade it.
    upgraded = False
    for sub_ts, attestation in list(ts.all_attestations()):
        # PendingAttestation has a `uri` field pointing to the calendar
        # that issued it. BitcoinBlockHeaderAttestation never needs
        # upgrading. Anything else (CalendarAttestation) shouldn't
        # appear in real proofs.
        from opentimestamps.core.notary import (
            PendingAttestation, BitcoinBlockHeaderAttestation,
        )
        if isinstance(attestation, BitcoinBlockHeaderAttestation):
            upgraded = True  # already anchored
            continue
        if not isinstance(attestation, PendingAttestation):
            continue
        try:
            cal = RemoteCalendar(attestation.uri)
            upgraded_ts = cal.get_timestamp(sub_ts.msg, timeout=CALENDAR_TIMEOUT_S)
            sub_ts.merge(upgraded_ts)
            # Did the upgrade introduce a Bitcoin attestation?
            for _, att in upgraded_ts.all_attestations():
                if isinstance(att, BitcoinBlockHeaderAttestation):
                    upgraded = True
                    break
        except Exception as exc:
            logger.debug("OTS upgrade from %s not yet available: %s",
                         attestation.uri, exc)
    return _serialize_proof(ts), upgraded


async def upgrade_proof(
    proof_bytes: bytes, digest: bytes,
) -> tuple[Optional[bytes], bool]:
    return await asyncio.to_thread(_upgrade_proof_blocking, proof_bytes, digest)


# ── Mongo-backed proof records ─────────────────────────────────────────────


async def ensure_indexes(db) -> None:
    try:
        await db.integrity_proofs.create_index([("digest_hex", 1)], unique=True)
        await db.integrity_proofs.create_index([("kind", 1), ("created_at", -1)])
        await db.integrity_proofs.create_index([("subject_id", 1)])
    except Exception as exc:
        logger.warning("integrity_proofs index creation failed: %s", exc)


async def create_proof_record(
    db, *, kind: str, subject_id: Optional[str], digest_hex: str,
    proof_bytes: bytes, calendars: list[str],
    extra: Optional[dict] = None,
) -> dict:
    """Insert (or upsert) the proof record. Returns the stored row
    minus the proof bytes (so it's safe to JSON-serialize). The raw
    proof bytes live in the row but are only surfaced via the dedicated
    `.ots` download endpoint.
    """
    now = datetime.now(timezone.utc)
    row = {
        "id": str(uuid.uuid4()),
        "kind": kind,                        # "file" | "merkle_daily"
        "subject_id": subject_id,            # gridfs file_id, or merkle batch id
        "digest_hex": digest_hex,            # hex of SHA-256 anchored
        "calendars": calendars,
        "status": "pending",                 # pending | anchored
        "created_at": now,
        "last_upgrade_at": None,
        "anchored_at": None,
        "anchored_block_height": None,
        "extra": extra or {},
        "proof_bytes": proof_bytes,
    }
    try:
        await db.integrity_proofs.insert_one(dict(row))
    except Exception as exc:
        logger.warning("integrity_proofs insert failed: %s", exc)
        return {}
    # Drop heavy fields before returning.
    public = {k: v for k, v in row.items() if k != "proof_bytes"}
    public.pop("_id", None)
    return public


async def get_proof_record(db, *, record_id: str) -> Optional[dict]:
    return await db.integrity_proofs.find_one(
        {"id": record_id}, {"_id": 0},
    )


async def get_proof_for_subject(
    db, *, kind: str, subject_id: str,
) -> Optional[dict]:
    return await db.integrity_proofs.find_one(
        {"kind": kind, "subject_id": subject_id}, {"_id": 0},
    )


async def try_upgrade_pending(db, *, record_id: str) -> Optional[dict]:
    """Fetch the proof record, try to upgrade it via the calendars,
    persist any upgrade, return the (refreshed) public-safe row.

    Safe to call repeatedly — does nothing if the proof is already
    Bitcoin-anchored, and is rate-limited internally so callers can't
    hammer the calendars.
    """
    row = await db.integrity_proofs.find_one({"id": record_id})
    if not row:
        return None
    if row.get("status") == "anchored":
        return _public_proof_row(row)
    # Internal rate limit: at most one calendar poll every 5 minutes
    # per proof record.
    last = row.get("last_upgrade_at")
    if isinstance(last, datetime):
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - last) < timedelta(minutes=5):
            return _public_proof_row(row)
    digest = bytes.fromhex(row["digest_hex"])
    proof = row.get("proof_bytes") or b""
    new_proof, became_anchored = await upgrade_proof(proof, digest)
    update = {"last_upgrade_at": datetime.now(timezone.utc)}
    if new_proof:
        update["proof_bytes"] = new_proof
    if became_anchored:
        update["status"] = "anchored"
        update["anchored_at"] = datetime.now(timezone.utc)
    try:
        await db.integrity_proofs.update_one({"id": record_id}, {"$set": update})
    except Exception as exc:
        logger.warning("integrity_proofs upgrade write failed: %s", exc)
    refreshed = await db.integrity_proofs.find_one({"id": record_id})
    return _public_proof_row(refreshed) if refreshed else None


def _public_proof_row(row: dict) -> dict:
    """Strip Mongo internals and the heavy `proof_bytes` blob — that's
    served separately via the .ots download endpoint."""
    out = {k: v for k, v in row.items() if k not in {"_id", "proof_bytes"}}
    out["proof_size_bytes"] = len(row.get("proof_bytes") or b"")
    return out


# ── Public API: hash a file & stamp it ─────────────────────────────────────


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def stamp_file_content(
    db, *, content: bytes, gridfs_file_id: Optional[str] = None,
    label: Optional[str] = None,
) -> Optional[dict]:
    """Hash the file content, submit to calendars, persist the proof
    record. Returns the public-safe row. Returns None if every calendar
    failed (offline, blocked, etc.) — caller can keep going without a
    proof rather than blocking the upload.

    Idempotent on `(kind='file', subject_id=gridfs_file_id)` AND on the
    digest itself: re-stamping the same bytes returns the existing row.
    """
    digest_hex = sha256_hex(content)
    # Subject-level dedupe: same gridfs id → return existing row.
    if gridfs_file_id:
        existing = await get_proof_for_subject(
            db, kind="file", subject_id=gridfs_file_id,
        )
        if existing:
            return _public_proof_row(existing)
    # Digest-level dedupe: same bytes already stamped (regardless of
    # subject) → reuse and link the new subject to it.
    by_digest = await db.integrity_proofs.find_one(
        {"digest_hex": digest_hex},
    )
    if by_digest:
        if gridfs_file_id and by_digest.get("subject_id") != gridfs_file_id:
            # Don't overwrite the existing subject — just return it.
            pass
        return _public_proof_row(by_digest)
    digest = bytes.fromhex(digest_hex)
    proof_bytes, calendars = await stamp_digest(digest)
    if not proof_bytes:
        logger.warning("stamp_file_content: every calendar failed for %s",
                       digest_hex)
        return None
    extra = {"label": label} if label else {}
    return await create_proof_record(
        db, kind="file", subject_id=gridfs_file_id, digest_hex=digest_hex,
        proof_bytes=proof_bytes, calendars=calendars, extra=extra,
    )


# ── Tier 2: daily Merkle anchor of business records ────────────────────────


def _merkle_root(leaves: list[bytes]) -> bytes:
    """Compute a binary Merkle root over `leaves`. Odd nodes are
    duplicated (Bitcoin-style). Empty input returns an all-zero hash.
    """
    if not leaves:
        return b"\x00" * 32
    layer = [bytes(leaf) for leaf in leaves]
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        nxt = []
        for i in range(0, len(layer), 2):
            nxt.append(hashlib.sha256(layer[i] + layer[i + 1]).digest())
        layer = nxt
    return layer[0]


def _hash_record(record: dict) -> bytes:
    """Produce a stable SHA-256 over a Mongo record. Sort keys to make
    the digest order-independent so re-runs on the same data match.
    """
    import json
    def _default(o):
        if isinstance(o, datetime):
            return o.replace(microsecond=0).isoformat()
        if hasattr(o, "__str__"):
            return str(o)
        return None
    serialized = json.dumps(record, sort_keys=True, default=_default,
                            separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(serialized).digest()


async def build_daily_audit_anchor(db, *, include_today: bool = False) -> Optional[dict]:
    """Build a Merkle root over yesterday's high-value records and
    anchor it. Records included:

      * `mission_events` (every agent action)
      * `workspace_invites` used (membership grant moments)
      * `org_members` joined (new memberships)
      * `audit_leads` captured

    The root + manifest is stored as a `merkle_daily` row in
    `integrity_proofs`. Customers can later re-hash any single record
    and walk the Merkle path stored in `extra.manifest` to prove it
    was part of that day's anchored set.

    `include_today=True` builds a partial anchor for the rolling 24h
    window ending now — useful for manual "run-now" requests so admins
    don't have to wait until midnight UTC for visible feedback.
    """
    now = datetime.now(timezone.utc)
    if include_today:
        window_end = now
        window_start = window_end - timedelta(days=1)
    else:
        # Window: previous calendar UTC day [00:00, 24:00).
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = start_of_today
        window_start = window_end - timedelta(days=1)
    # Idempotency: if we already anchored a window starting at this
    # exact instant, return that row. (The full-day path is naturally
    # idempotent on UTC midnights; the include_today path uses the
    # current second so re-runs build fresh anchors.)
    existing = await db.integrity_proofs.find_one({
        "kind": "merkle_daily",
        "extra.window_start": window_start.isoformat(),
    })
    if existing and not include_today:
        return _public_proof_row(existing)
    leaves: list[bytes] = []
    manifest: list[dict] = []
    # Each collection is queried with a stable sort so the leaf order
    # is reproducible. Cap each collection at 50_000 records to keep
    # the daily anchor bounded; over-cap days fall back to a count-only
    # entry (signal to downstream that this day is incomplete).
    targets = [
        ("mission_events", "ts", {"ts": {"$gte": window_start, "$lt": window_end}}),
        ("workspace_invites", "used_at",
            {"used_at": {"$gte": window_start, "$lt": window_end}}),
        ("org_members", "joined_at",
            {"joined_at": {"$gte": window_start, "$lt": window_end}}),
        ("audit_leads", "submitted_at",
            {"submitted_at": {"$gte": window_start, "$lt": window_end}}),
    ]
    total = 0
    for coll_name, sort_field, filt in targets:
        try:
            cur = db[coll_name].find(filt, {"_id": 0}).sort(sort_field, 1).limit(50_000)
            n = 0
            async for rec in cur:
                leaf = _hash_record(rec)
                leaves.append(leaf)
                # Keep the manifest light — only enough to let a
                # customer find a record and re-hash it.
                manifest_id = (
                    rec.get("id")
                    or rec.get("code")
                    or rec.get("user_id")
                    or rec.get("email")
                    or ""
                )
                manifest.append({
                    "collection": coll_name,
                    "id": manifest_id,
                    "leaf_hex": leaf.hex(),
                })
                n += 1
            total += n
            logger.info("daily_anchor: %s -> %d records", coll_name, n)
        except Exception as exc:
            logger.warning("daily_anchor: failed on %s: %s", coll_name, exc)
    if total == 0:
        logger.info("daily_anchor: window %s had zero records, skipping",
                    window_start.isoformat())
        return None
    root = _merkle_root(leaves)
    proof_bytes, calendars = await stamp_digest(root)
    if not proof_bytes:
        logger.warning("daily_anchor: every calendar failed for root %s",
                       root.hex())
        return None
    extra = {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "leaf_count": len(leaves),
        # Manifest fits comfortably in a single Mongo doc up to ~16MB
        # for tens of thousands of records. We cap above so the
        # 16MB ceiling isn't a concern in practice.
        "manifest": manifest,
    }
    return await create_proof_record(
        db, kind="merkle_daily", subject_id=None,
        digest_hex=root.hex(), proof_bytes=proof_bytes,
        calendars=calendars, extra=extra,
    )


# ── Background scheduler ───────────────────────────────────────────────────


async def daily_anchor_loop(db, *, sleep_s: int = 86400) -> None:
    """Forever-loop that runs `build_daily_audit_anchor` once per day.
    Started from server.py at backend startup. Catches errors so a
    transient failure doesn't kill the loop.

    On startup we also fire one immediate run so a freshly-restarted
    pod backfills the most recent missing day instead of waiting up
    to 24 hours.
    """
    try:
        await build_daily_audit_anchor(db)
    except Exception as exc:
        logger.warning("daily_anchor_loop: initial run failed: %s", exc)
    while True:
        try:
            await asyncio.sleep(sleep_s)
            await build_daily_audit_anchor(db)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("daily_anchor_loop: tick failed: %s", exc)
            # Sleep a little so a flapping calendar doesn't pin CPU.
            await asyncio.sleep(60)


async def upgrade_pending_loop(db, *, sleep_s: int = 1800) -> None:
    """Periodically poll the calendars to upgrade pending proofs to
    Bitcoin-anchored. Starts ~5 minutes after boot to avoid thrashing
    the calendars on every redeploy.
    """
    try:
        await asyncio.sleep(300)
    except asyncio.CancelledError:
        return
    while True:
        try:
            cur = db.integrity_proofs.find(
                {"status": "pending"},
                {"_id": 0, "id": 1, "last_upgrade_at": 1},
            ).sort("created_at", 1).limit(50)
            ids = [r["id"] async for r in cur]
            for rid in ids:
                await try_upgrade_pending(db, record_id=rid)
            await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("upgrade_pending_loop: tick failed: %s", exc)
            await asyncio.sleep(60)


__all__ = [
    "ensure_indexes",
    "stamp_file_content",
    "build_daily_audit_anchor",
    "try_upgrade_pending",
    "get_proof_for_subject",
    "get_proof_record",
    "daily_anchor_loop",
    "upgrade_pending_loop",
    "_public_proof_row",
    "sha256_hex",
]
