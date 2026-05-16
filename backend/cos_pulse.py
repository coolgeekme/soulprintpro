"""Chief-of-Staff "Pulse" — proactive nudges sent to users via Telegram.

Phase 1 strategy (deliberate, narrow):
  Don't run an LLM call per user×org per hour. That's wasteful and
  generates LLM-flavoured spam. Instead, scan structured events that
  the user has already explicitly cared about, decide whether anything
  is *demonstrably* worth surfacing, and only then assemble a nudge.

Phase-1 trigger sources (highest-signal first):
  1. **Mission completions**  any mission whose `status` flipped to
     `succeeded` or `failed` in the last sweep window. Both outcomes
     deserve a heads-up: success → review the artifacts; failure →
     investigate / retry.
  2. **Social-post failures**  any social_post that finished with
     `status in {failed, partial}` since the last sweep. Tells the
     user a publish hit a real Meta / LinkedIn error.
  3. **Scheduled posts going live**  any post that *just* published
     successfully (so they can confirm it on the feed).
  4. **Stalled drafts**  social_posts in `draft` for >7 days and
     never published. Light nudge to retire or schedule them.

We track `last_sweep_at` per user so a single APScheduler tick scans
*only* the delta since the last successful nudge round. That keeps
the cost bounded no matter how many users we onboard.

Phase 3 hook: `handle_inbound_message` routes any Telegram message
back to the right org's Chief of Staff conversation. Currently a
lightweight stub that records the message and ACKs — full LLM
round-trip from Telegram is wired in Phase 3.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId

import telegram_bot

logger = logging.getLogger("foundry.cos_pulse")

# Default sweep window. APScheduler fires hourly; we look at the last
# 70 minutes so a brief delay (auto-archive, watchdog) doesn't drop
# an event. Per-user `last_sweep_at` overrides this when available.
DEFAULT_LOOKBACK = timedelta(minutes=70)

# Stalled-draft threshold — only surfaced once per draft via the
# `pulse_nudges` audit collection.
STALLED_DRAFT_AGE = timedelta(days=7)


# ── Audit collection ────────────────────────────────────────────────────────
# Every nudge we send is logged so we never double-fire on the same
# trigger after a server restart and so we can introspect "why did
# Reggie get pinged about mission X but not mission Y?" later. Indexed
# on (user_id, kind, ref_id) for cheap lookups.
async def _already_nudged(db, user_id: str, kind: str, ref_id: str) -> bool:
    if not ref_id:
        return False
    found = await db.pulse_nudges.find_one(
        {"user_id": user_id, "kind": kind, "ref_id": ref_id},
        {"_id": 1},
    )
    return bool(found)


async def _record_nudge(
    db, *, user_id: str, org_id: str, kind: str, ref_id: str,
    text: str, chat_id: int, ok: bool, error: Optional[str] = None,
) -> None:
    await db.pulse_nudges.insert_one({
        "user_id": user_id,
        "org_id": org_id,
        "kind": kind,
        "ref_id": ref_id,
        "text_preview": (text or "")[:400],
        "chat_id": chat_id,
        "delivered": ok,
        "error": error,
        "at": datetime.now(timezone.utc),
    })


async def _user_orgs(db, user_id: str) -> List[Dict[str, Any]]:
    """Every org this user is a member of. We need `name` for the
    `[OrgName]` prefix the nudges carry."""
    out: List[Dict[str, Any]] = []
    cur = db.org_members.find({"user_id": user_id}, {"_id": 0, "org_id": 1, "role": 1})
    async for m in cur:
        try:
            org = await db.organizations.find_one(
                {"_id": ObjectId(m["org_id"])}, {"_id": 1, "name": 1},
            )
        except Exception:
            continue
        if org:
            out.append({"id": str(org["_id"]), "name": org.get("name", ""), "role": m.get("role")})
    return out


# ── Per-trigger scanners ────────────────────────────────────────────────────
async def _scan_mission_events(
    db, user_id: str, org: Dict[str, Any], since: datetime,
) -> List[Dict[str, Any]]:
    """Mission status transitions to succeeded/failed since `since`.
    Owner-only orgs: every member is the implicit assignee, so we
    nudge for any mission. Multi-member orgs: only nudge for missions
    whose `created_by` matches the user (avoids spamming a teammate
    about missions they didn't own).
    """
    nudges: List[Dict[str, Any]] = []
    q: Dict[str, Any] = {
        "org_id": org["id"],
        "status": {"$in": ["succeeded", "failed"]},
        "finished_at": {"$gte": since},
        "created_by": user_id,
    }
    async for m in db.missions.find(q, {"_id": 1, "objective": 1, "status": 1}):
        nudges.append({
            "kind": f"mission_{m['status']}",
            "ref_id": str(m["_id"]),
            "body": (
                ("✅ Mission succeeded · " if m["status"] == "succeeded" else "⚠️ Mission failed · ")
                + (m.get("objective") or "Unnamed mission")[:200]
            ),
            "action": "Open Missions in Foundry to review.",
        })
    return nudges


async def _scan_social_failures(
    db, user_id: str, org: Dict[str, Any], since: datetime,
) -> List[Dict[str, Any]]:
    nudges: List[Dict[str, Any]] = []
    q = {
        "org_id": org["id"],
        "author_id": user_id,
        "status": {"$in": ["failed", "partial"]},
        "publish_finished_at": {"$gte": since},
    }
    async for p in db.social_posts.find(q, {"_id": 1, "text": 1, "status": 1, "failed_count": 1, "publish_results": 1}):
        # Extract the most informative error message from publish_results.
        first_err = ""
        for plat, res in (p.get("publish_results") or {}).items():
            if not (res or {}).get("ok") and (res or {}).get("error"):
                first_err = f"{plat}: {(res or {}).get('error', '')[:120]}"
                break
        nudges.append({
            "kind": f"post_{p['status']}",
            "ref_id": str(p["_id"]),
            "body": (
                f"⚠️ Social post {p['status']} · "
                f"{(p.get('text') or '')[:100]}…\n"
                + (f"<i>{telegram_bot.escape_html(first_err)}</i>" if first_err else "")
            ),
            "action": "Open Social Queue → tap the post for full details.",
        })
    return nudges


async def _scan_social_published(
    db, user_id: str, org: Dict[str, Any], since: datetime,
) -> List[Dict[str, Any]]:
    nudges: List[Dict[str, Any]] = []
    q = {
        "org_id": org["id"],
        "author_id": user_id,
        "status": "published",
        "published_at": {"$gte": since},
    }
    async for p in db.social_posts.find(q, {"_id": 1, "text": 1, "platforms": 1}):
        plats = ", ".join(p.get("platforms") or []) or "social"
        nudges.append({
            "kind": "post_published",
            "ref_id": str(p["_id"]),
            "body": (
                f"📣 Posted to {plats} · "
                f"{(p.get('text') or '')[:120]}…"
            ),
            "action": "Tap to view in Foundry.",
        })
    return nudges


async def _scan_stalled_drafts(
    db, user_id: str, org: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """A single nudge per stalled draft, per user — `_already_nudged`
    handles the dedupe. We deliberately don't repeat the reminder if
    it's already been sent; the user can find the draft via the
    Social Queue page if they want."""
    nudges: List[Dict[str, Any]] = []
    cutoff = datetime.now(timezone.utc) - STALLED_DRAFT_AGE
    q = {
        "org_id": org["id"],
        "author_id": user_id,
        "status": "draft",
        "created_at": {"$lt": cutoff},
    }
    async for p in db.social_posts.find(q, {"_id": 1, "text": 1, "created_at": 1}).limit(10):
        age_days = (datetime.now(timezone.utc) - p["created_at"]).days
        nudges.append({
            "kind": "draft_stalled",
            "ref_id": str(p["_id"]),
            "body": (
                f"📝 Draft idle for {age_days}d · "
                f"{(p.get('text') or '')[:120]}…"
            ),
            "action": "Schedule it, publish it, or archive it.",
        })
    return nudges


# ── Sweep entry point ───────────────────────────────────────────────────────
async def pulse_for_user(db, user_doc: Dict[str, Any]) -> int:
    """Run a full pulse for ONE user. Returns the number of nudges
    actually delivered (so the global tick can log progress).
    """
    uid = str(user_doc["_id"])
    tg = user_doc.get("telegram") or {}
    chat_id = tg.get("chat_id")
    prefs = tg.get("preferences") or {}
    if not chat_id or prefs.get("outbound_enabled") is False:
        return 0
    last_sweep = tg.get("last_pulse_at")
    if not isinstance(last_sweep, datetime):
        last_sweep = datetime.now(timezone.utc) - DEFAULT_LOOKBACK
    # Floor at 12h ago so a long outage doesn't dump a 3-day backlog
    # of nudges all at once when the worker comes back up.
    floor = datetime.now(timezone.utc) - timedelta(hours=12)
    if last_sweep < floor:
        last_sweep = floor

    orgs = await _user_orgs(db, uid)
    if not orgs:
        return 0

    delivered = 0
    for org in orgs:
        events: List[Dict[str, Any]] = []
        events.extend(await _scan_mission_events(db, uid, org, last_sweep))
        events.extend(await _scan_social_failures(db, uid, org, last_sweep))
        events.extend(await _scan_social_published(db, uid, org, last_sweep))
        events.extend(await _scan_stalled_drafts(db, uid, org))
        for ev in events:
            if await _already_nudged(db, uid, ev["kind"], ev["ref_id"]):
                continue
            text = telegram_bot.format_nudge(
                org_label=org["name"],
                body=ev["body"],
                action_label=ev.get("action"),
            )
            ok = False
            err: Optional[str] = None
            try:
                await telegram_bot.send_throttled(chat_id, text)
                ok = True
                delivered += 1
            except Exception as exc:
                err = str(exc)
            await _record_nudge(
                db,
                user_id=uid, org_id=org["id"],
                kind=ev["kind"], ref_id=ev["ref_id"],
                text=text, chat_id=chat_id, ok=ok, error=err,
            )

    # Stamp last_pulse_at so the next sweep starts where this one left
    # off (independent of APScheduler's cron cadence — survives misses).
    await db.users.update_one(
        {"_id": user_doc["_id"]},
        {"$set": {"telegram.last_pulse_at": datetime.now(timezone.utc)}},
    )
    return delivered


async def pulse_tick(db) -> Dict[str, int]:
    """Top-level scheduler entry — runs once per APScheduler tick.

    Iterates every user with a connected Telegram chat. Errors on one
    user never block the rest. Returns a small summary dict that the
    scheduler logs on success.
    """
    scanned = 0
    delivered = 0
    cur = db.users.find(
        {"telegram.chat_id": {"$ne": None, "$exists": True}},
        {"_id": 1, "telegram": 1, "name": 1, "email": 1},
    )
    async for user in cur:
        scanned += 1
        try:
            delivered += await pulse_for_user(db, user)
        except Exception:
            logger.exception("pulse_for_user failed for %s", user.get("_id"))
    if delivered or scanned:
        logger.info("cos_pulse: scanned=%d delivered=%d", scanned, delivered)
    return {"scanned": scanned, "delivered": delivered}


# ── Phase 3 inbound stub (full implementation in Phase 3) ───────────────────
async def handle_inbound_message(
    db, *, chat_id: int, text: str, update: Dict[str, Any],
) -> None:
    """Phase 1 stub. Records the inbound message so we can review what
    users tried to say before Phase 3 lands, and sends a short ack so
    the chat doesn't feel dead.

    Phase 3 will replace this with: parse org-context → route message
    into the right Chief-of-Staff conversation → run the agent → send
    the response back via send_message.
    """
    user_doc = await db.users.find_one(
        {"telegram.chat_id": chat_id},
        {"_id": 1, "name": 1, "email": 1},
    )
    await db.telegram_inbound_log.insert_one({
        "chat_id": chat_id,
        "user_id": str(user_doc["_id"]) if user_doc else None,
        "text": text[:2000],
        "at": datetime.now(timezone.utc),
    })
    await telegram_bot.safe_send(
        chat_id,
        telegram_bot.format_nudge(
            org_label="Foundry",
            body=(
                "Got it — I've logged your message. Two-way Telegram chat with your "
                "Chief of Staff is coming in the next phase; for now you'll still "
                "get my proactive nudges in this chat.\n\n"
                "Tip: open Foundry in your browser to chat with your CoS today."
            ),
        ),
    )


# Ensure the audit indexes exist at first import. Idempotent — Motor's
# create_index returns immediately if the index already exists.
async def ensure_indexes(db) -> None:
    try:
        await db.pulse_nudges.create_index([("user_id", 1), ("kind", 1), ("ref_id", 1)])
        await db.pulse_nudges.create_index([("at", -1)])
        await db.telegram_inbound_log.create_index([("at", -1)])
    except Exception:
        logger.exception("cos_pulse: failed to create indexes")
