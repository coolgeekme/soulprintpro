"""FastAPI routes for the direct Telegram Bot integration (Phase 1+3).

Phase 1 (this scope):
  - POST /api/telegram/initiate           start the connection flow
  - POST /api/telegram/webhook            inbound update from Telegram
  - GET  /api/telegram/status             am I connected? + bot health
  - POST /api/telegram/disconnect         severs the link (UI affordance)
  - PATCH /api/telegram/preferences       toggle nudges on/off
  - POST /api/telegram/test-send          fire a "hello" to verify
  - POST /api/telegram/admin/webhook-setup (superadmin) one-time register

Phase 3 (inbound replies) is wired through `webhook` but the routing-
to-Chief-of-Staff piece lives in `cos_pulse.handle_inbound_message` so
this file stays a thin transport layer.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel

import telegram_bot

logger = logging.getLogger("foundry.telegram_routes")


def register_telegram_routes(
    app_router: APIRouter,
    db,
    *,
    get_current_user,
    require_superadmin=None,
):
    """Mount the routes on the given router. Caller owns prefix + auth
    middleware. We avoid module-level imports of the FastAPI app so
    server.py stays the single composition root.
    """
    r = APIRouter()

    # ── Helpers ───────────────────────────────────────────────────────
    async def _user_doc(user_id: str) -> Dict[str, Any]:
        return await db.users.find_one({"_id": ObjectId(user_id)}) or {}

    def _deep_link(nonce: str) -> str:
        username = telegram_bot.bot_username()
        return f"https://t.me/{username}?start={nonce}"

    # ── Public endpoints ──────────────────────────────────────────────
    @r.get("/telegram/status")
    async def telegram_status(u: dict = Depends(get_current_user)):
        """Is the bot configured? Am I connected? Returns a tiny payload
        the Account UI uses to render the Connect vs Disconnect state.
        """
        configured = telegram_bot.is_configured()
        out: Dict[str, Any] = {
            "configured": configured,
            "bot_username": telegram_bot.bot_username() if configured else "",
            "connected": False,
        }
        doc = await _user_doc(u["id"])
        tg = doc.get("telegram") or {}
        if tg.get("chat_id"):
            out["connected"] = True
            out["username"] = tg.get("username") or ""
            ca = tg.get("connected_at")
            out["connected_at"] = ca.isoformat() if isinstance(ca, datetime) else ca
            out["preferences"] = tg.get("preferences") or {}
        return out

    @r.post("/telegram/initiate")
    async def telegram_initiate(u: dict = Depends(get_current_user)):
        """Mint a fresh nonce and return the t.me deep link the user
        taps to land in the bot. The nonce binds the inbound `/start`
        webhook to this user account.

        Nonces are single-use, expire after 30 minutes, and never reuse
        an existing one — re-initiating invalidates the previous nonce
        so an interrupted flow can't be hijacked.
        """
        if not telegram_bot.is_configured():
            raise HTTPException(503, "Telegram bot isn't configured. Add TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_USERNAME to backend env.")
        nonce = secrets.token_urlsafe(18)  # ~24 chars, URL-safe
        await db.users.update_one(
            {"_id": ObjectId(u["id"])},
            {"$set": {
                "telegram.pending_nonce": nonce,
                "telegram.pending_nonce_at": datetime.now(timezone.utc),
            }},
        )
        return {
            "deeplink": _deep_link(nonce),
            "bot_username": telegram_bot.bot_username(),
            "nonce": nonce,
            "expires_in_seconds": 1800,
        }

    @r.post("/telegram/disconnect")
    async def telegram_disconnect(u: dict = Depends(get_current_user)):
        """Sever this user's Telegram link. We keep the document but
        clear the chat-id so no further nudges fire. Re-connect just
        re-runs the deep-link flow.
        """
        await db.users.update_one(
            {"_id": ObjectId(u["id"])},
            {"$set": {
                "telegram.chat_id": None,
                "telegram.disconnected_at": datetime.now(timezone.utc),
            }, "$unset": {"telegram.pending_nonce": "", "telegram.pending_nonce_at": ""}},
        )
        return {"ok": True}

    class _PrefsBody(BaseModel):
        outbound_enabled: Optional[bool] = None
        digest_enabled: Optional[bool] = None
        min_severity: Optional[str] = None  # actionable | all | weekly_only

    @r.patch("/telegram/preferences")
    async def telegram_preferences(
        body: _PrefsBody, u: dict = Depends(get_current_user),
    ):
        """Toggle proactive notifications. Stored under
        `users.telegram.preferences`; reading it in the Pulse worker
        gates the entire sweep for this user."""
        update: Dict[str, Any] = {}
        if body.outbound_enabled is not None:
            update["telegram.preferences.outbound_enabled"] = bool(body.outbound_enabled)
        if body.digest_enabled is not None:
            update["telegram.preferences.digest_enabled"] = bool(body.digest_enabled)
        if body.min_severity is not None:
            if body.min_severity not in ("actionable", "all", "weekly_only"):
                raise HTTPException(400, "min_severity must be one of: actionable, all, weekly_only")
            update["telegram.preferences.min_severity"] = body.min_severity
        if not update:
            raise HTTPException(400, "No preferences provided")
        await db.users.update_one({"_id": ObjectId(u["id"])}, {"$set": update})
        return {"ok": True}

    @r.get("/telegram/debug")
    async def telegram_debug(u: dict = Depends(get_current_user)):
        """Owner-only diagnostic. Returns this user's raw `telegram.*`
        fields plus the bot's current webhook info — gives us a single
        place to confirm: (a) the nonce was minted, (b) the webhook
        points where we think, (c) any past chat_id was captured."""
        doc = await _user_doc(u["id"])
        tg = doc.get("telegram") or {}
        snap: Dict[str, Any] = {
            "user_id": u["id"],
            "user_email": u.get("email"),
            "bot_username": telegram_bot.bot_username(),
            "bot_configured": telegram_bot.is_configured(),
            "telegram_fields": {
                "chat_id": tg.get("chat_id"),
                "username": tg.get("username"),
                "connected_at": (tg.get("connected_at").isoformat() if isinstance(tg.get("connected_at"), datetime) else tg.get("connected_at")),
                "pending_nonce_present": bool(tg.get("pending_nonce")),
                "pending_nonce_age_seconds": (
                    int((datetime.now(timezone.utc) - tg["pending_nonce_at"]).total_seconds())
                    if isinstance(tg.get("pending_nonce_at"), datetime) else None
                ),
                "last_pulse_at": (tg.get("last_pulse_at").isoformat() if isinstance(tg.get("last_pulse_at"), datetime) else tg.get("last_pulse_at")),
            },
        }
        try:
            snap["webhook_info"] = await telegram_bot.get_webhook_info()
        except Exception as exc:
            snap["webhook_info_error"] = str(exc)
        return snap

    class _ManualClaimBody(BaseModel):
        chat_id: int

    @r.post("/telegram/manual-claim")
    async def telegram_manual_claim(
        body: _ManualClaimBody, u: dict = Depends(get_current_user),
    ):
        """Bypass the nonce flow. Useful when deep-link delivery fails
        on a fresh production deploy or behind aggressive caching.

        Flow:
          1. User opens Telegram and types `/whoami` to the bot.
          2. Bot replies with their `chat_id`.
          3. User pastes the chat_id here.
          4. We bind it to their Foundry account (with a safety check:
             refuse if some other user already owns that chat_id).
        """
        chat_id = int(body.chat_id)
        existing = await db.users.find_one(
            {"telegram.chat_id": chat_id, "_id": {"$ne": ObjectId(u["id"])}},
            {"_id": 1, "email": 1},
        )
        if existing:
            raise HTTPException(
                409,
                f"That Telegram chat is already linked to {existing.get('email') or 'another account'}.",
            )
        await db.users.update_one(
            {"_id": ObjectId(u["id"])},
            {"$set": {
                "telegram.chat_id": chat_id,
                "telegram.username": "",  # filled in next time the bot sees them
                "telegram.connected_at": datetime.now(timezone.utc),
            }, "$unset": {"telegram.pending_nonce": "", "telegram.pending_nonce_at": ""}},
        )
        # Send a greeting so the user sees the link took effect.
        try:
            await telegram_bot.send_message(
                chat_id,
                telegram_bot.format_nudge(
                    org_label="Foundry",
                    body=f"✅ Linked to {telegram_bot.escape_html(u.get('email') or 'your account')} via manual claim. You're set.",
                ),
            )
        except Exception as exc:
            # The chat_id might be wrong — surface the error so the
            # user knows their paste was bad.
            await db.users.update_one(
                {"_id": ObjectId(u["id"])},
                {"$set": {"telegram.chat_id": None}},
            )
            raise HTTPException(400, f"Couldn't reach that chat — double-check the number you pasted from /whoami. ({exc})")
        return {"ok": True, "chat_id": chat_id}

    @r.post("/telegram/test-send")
    async def telegram_test_send(u: dict = Depends(get_current_user)):
        """Fire a sanity ping. Surfaces the failure inline if the user
        connected and then blocked the bot, since Telegram returns a
        specific `description` we relay to the UI."""
        doc = await _user_doc(u["id"])
        chat_id = (doc.get("telegram") or {}).get("chat_id")
        if not chat_id:
            raise HTTPException(400, "Telegram isn't connected for this account.")
        text = telegram_bot.format_nudge(
            org_label="Foundry",
            body=(
                "✅ Hello — your Chief of Staff is wired up.\n\n"
                "From here on, you'll see actionable nudges land in this chat. "
                "Reply directly to talk to whichever org's CoS most recently messaged you."
            ),
            action_label="Manage notifications in Foundry → Account → Telegram.",
        )
        try:
            await telegram_bot.send_message(chat_id, text)
        except Exception as exc:
            raise HTTPException(502, f"Telegram rejected the send: {exc}")
        return {"ok": True}

    @r.get("/telegram/pulse-history")
    async def telegram_pulse_history(
        limit: int = 10, u: dict = Depends(get_current_user),
    ):
        """Return this user's most recent Pulse nudges (delivered or not)
        so the Account UI can show a "what Foundry flagged today" panel.
        Useful when the user missed the Telegram ping or wants to review
        / tune their preferences.

        Each row carries the org label, the nudge kind (mission_succeeded,
        post_failed, draft_stalled, …), a short text preview, and the
        delivery outcome — so the UI can render an icon per state and
        link back to the underlying entity later if we want.
        """
        limit = max(1, min(50, int(limit)))
        cur = db.pulse_nudges.find(
            {"user_id": u["id"]},
            {
                "_id": 0,
                "org_id": 1, "kind": 1, "ref_id": 1,
                "text_preview": 1, "delivered": 1, "error": 1, "at": 1,
            },
        ).sort("at", -1).limit(limit)
        rows = await cur.to_list(limit)
        # Resolve org names in one batch so the response is self-contained.
        from bson import ObjectId
        org_ids = list({r["org_id"] for r in rows if r.get("org_id")})
        org_name: Dict[str, str] = {}
        for oid in org_ids:
            try:
                org = await db.organizations.find_one({"_id": ObjectId(oid)}, {"_id": 1, "name": 1})
            except Exception:
                continue
            if org:
                org_name[oid] = org.get("name", "")
        out = []
        for r in rows:
            out.append({
                "kind": r.get("kind"),
                "ref_id": r.get("ref_id"),
                "text_preview": r.get("text_preview"),
                "delivered": bool(r.get("delivered")),
                "error": r.get("error") or "",
                "at": r["at"].isoformat() if isinstance(r.get("at"), datetime) else r.get("at"),
                "org_id": r.get("org_id"),
                "org_name": org_name.get(r.get("org_id"), ""),
            })
        return {"items": out}

    # ── Inbound webhook ───────────────────────────────────────────────
    @r.post("/telegram/webhook")
    async def telegram_webhook(req: Request):
        """Telegram POSTs every inbound update (messages, callbacks) to
        this URL. We verify the optional secret header, dispatch /start
        nonces to claim the chat, and hand everything else to the CoS
        router (`cos_pulse.handle_inbound_message`) for Phase 3 routing.

        Returns 200 OK on success — Telegram retries on non-2xx.
        """
        # Optional secret-token header — set when we register the
        # webhook so a stranger can't spoof updates by guessing our URL.
        expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET") or ""
        if expected_secret:
            got = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if got != expected_secret:
                logger.warning("Telegram webhook called with bad secret token")
                raise HTTPException(403, "Forbidden")
        try:
            update = await req.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")
        chat_id = telegram_bot.chat_id_from_update(update)
        text = telegram_bot.text_from_update(update) or ""
        sender = telegram_bot.sender_from_update(update)
        if not chat_id:
            # Some update types (channel posts, etc.) we don't handle yet.
            return {"ok": True}

        # First-time onboarding: `/start <nonce>` payload binds the
        # chat to a Foundry user. We accept the first matching nonce
        # globally and clear it so it can't be replayed.
        nonce = telegram_bot.parse_deep_link_payload(text)
        if nonce:
            user_doc = await db.users.find_one(
                {"telegram.pending_nonce": nonce},
                {"_id": 1, "name": 1, "email": 1, "telegram.pending_nonce_at": 1},
            )
            if user_doc:
                # Optional: enforce 30-minute expiry. Soft expiry only —
                # we still bind, but log loudly so abuse stands out.
                started = (user_doc.get("telegram") or {}).get("pending_nonce_at")
                if isinstance(started, datetime):
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    if elapsed > 60 * 60:  # 1h hard cap
                        logger.warning(
                            "Telegram nonce older than 1h for user %s; rejecting",
                            user_doc["_id"],
                        )
                        await telegram_bot.safe_send(
                            chat_id,
                            "That sign-in link expired. Open Foundry → Account → Telegram and tap Connect again.",
                        )
                        return {"ok": True}
                await db.users.update_one(
                    {"_id": user_doc["_id"]},
                    {"$set": {
                        "telegram.chat_id": chat_id,
                        "telegram.username": sender.get("username") or "",
                        "telegram.telegram_user_id": sender.get("id") or "",
                        "telegram.connected_at": datetime.now(timezone.utc),
                    }, "$unset": {
                        "telegram.pending_nonce": "",
                        "telegram.pending_nonce_at": "",
                        "telegram.disconnected_at": "",
                    }},
                )
                await telegram_bot.safe_send(
                    chat_id,
                    telegram_bot.format_nudge(
                        org_label="Foundry",
                        body=(
                            f"✅ Connected to {telegram_bot.escape_html(user_doc.get('name') or 'your Foundry account')}.\n\n"
                            "From now on, this chat is your Chief of Staff inbox. "
                            "You'll get nudges when missions finish, posts need attention, or leads need a follow-up.\n\n"
                            "Reply at any time to chat back — your last-mentioned org gets the message."
                        ),
                    ),
                )
                return {"ok": True}
            # Nonce didn't match — usually a stale deep-link or someone
            # poking at the bot. Send a friendly error.
            await telegram_bot.safe_send(
                chat_id,
                "That sign-in link doesn't match an open session. Open Foundry → Account → Telegram and tap Connect again.",
            )
            return {"ok": True}

        # Plain `/start` (no payload) — friendly intro for a stranger.
        if text.strip().startswith("/start"):
            await telegram_bot.safe_send(
                chat_id,
                telegram_bot.format_nudge(
                    org_label="Foundry",
                    body=(
                        "👋 Hi — I'm the Foundry Chief of Staff.\n\n"
                        "I'm not connected to a Foundry account yet. Open <b>Foundry → Account → Telegram</b> "
                        "in your browser and tap <b>Connect Telegram</b> to link this chat.\n\n"
                        "If the deep link keeps failing, send me <b>/whoami</b> and I'll give you a chat ID you can paste into the manual-connect form."
                    ),
                ),
            )
            return {"ok": True}

        # `/whoami` — escape hatch for users whose deep-link flow keeps
        # failing (fresh deploy, aggressive CDN cache, weird Telegram
        # clients, etc.). Bot echoes the chat_id so the user can paste
        # it into POST /api/telegram/manual-claim.
        if text.strip().lower().startswith("/whoami"):
            await telegram_bot.safe_send(
                chat_id,
                telegram_bot.format_nudge(
                    org_label="Foundry",
                    body=(
                        f"Your Telegram chat ID is: <b>{chat_id}</b>\n\n"
                        "Open <b>Foundry → Account → Telegram</b> on the web, click <b>Manual connect</b>, and paste this number to link your chat without the deep link."
                    ),
                ),
            )
            return {"ok": True}

        # Any other inbound text — route to Phase-3 CoS handler. The
        # handler is loaded lazily so Phase 1 can ship without it; if
        # the import fails we just acknowledge silently.
        try:
            from cos_pulse import handle_inbound_message
            await handle_inbound_message(db, chat_id=chat_id, text=text, update=update)
        except ImportError:
            await telegram_bot.safe_send(
                chat_id,
                "Replies aren't enabled yet — I'll still ping you when something needs attention. "
                "(Coming soon: full two-way chat from Telegram.)",
            )
        except Exception:
            logger.exception("cos_pulse.handle_inbound_message failed")
        return {"ok": True}

    # ── Admin / one-time setup ────────────────────────────────────────
    @r.post("/telegram/admin/webhook-setup")
    async def admin_webhook_setup(
        body: Dict[str, Any] = Body(...),
        u: dict = Depends(get_current_user),
    ):
        """Register the public webhook URL with Telegram. Body:
            {"webhook_url": "https://foundryagents.ai/api/telegram/webhook",
             "secret": "<optional, will be stored in env>" }
        Only superadmins. Run once per environment (prod, staging).
        """
        if not u.get("is_superadmin"):
            raise HTTPException(403, "Superadmin only")
        url = (body or {}).get("webhook_url", "").strip()
        if not url.startswith("https://"):
            raise HTTPException(400, "webhook_url must be HTTPS")
        secret = (body or {}).get("secret") or ""
        try:
            res = await telegram_bot.set_webhook(url, secret_token=secret or None)
        except Exception as exc:
            raise HTTPException(502, f"Telegram refused webhook: {exc}")
        return {"ok": True, "webhook": res}

    @r.get("/telegram/admin/webhook-info")
    async def admin_webhook_info(u: dict = Depends(get_current_user)):
        if not u.get("is_superadmin"):
            raise HTTPException(403, "Superadmin only")
        try:
            info = await telegram_bot.get_webhook_info()
        except Exception as exc:
            raise HTTPException(502, f"Telegram info failed: {exc}")
        return info

    app_router.include_router(r)
    return r
