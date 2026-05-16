"""Direct Telegram Bot API client (no Composio dependency).

Why direct instead of via Composio?
  Telegram's Bot API uses a single, long-lived token — no OAuth, no
  refresh, no per-call quotas. For a proactive Chief of Staff that may
  fire dozens of nudges per user per day, paying Composio tool-call
  credits for every send is wasteful. This module wraps `httpx` calls
  to `https://api.telegram.org/bot<TOKEN>/...` directly. Free, fast,
  and unlimited within Telegram's natural rate limits (30 msgs/sec to
  distinct chats; 1 msg/sec to the same chat).

Phase 1 surface:
  - `send_message(chat_id, text, …)`     outbound nudge from the CoS
  - `set_webhook(url)`                  one-shot webhook bootstrap
  - `delete_webhook()`                  rollback / polling fallback
  - `get_me()`                          health check (used by /status)
  - `parse_deep_link_payload(text)`     parse `/start <nonce>` body

Higher-level orchestration (matching nonce → user, routing replies to
the right CoS conversation) lives in the FastAPI routes that consume
this module.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("foundry.telegram")

_API_BASE = "https://api.telegram.org"


def _token() -> str:
    """Return the bot token from env, or raise a clear error."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not tok:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Add it to backend/.env so the "
            "Chief of Staff can talk to Telegram."
        )
    return tok


def bot_username() -> str:
    """Username (without `@`) for building `t.me/<bot>` deep links."""
    return (os.environ.get("TELEGRAM_BOT_USERNAME") or "").lstrip("@").strip()


def is_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and bool(bot_username())


async def _request(method: str, payload: Dict[str, Any], *, timeout: float = 10.0) -> Dict[str, Any]:
    """POST to `bot<TOKEN>/<method>` and return Telegram's `result`.

    Raises on non-2xx OR Telegram-side `ok: false`. Callers should catch
    and translate to the user-facing error they need.
    """
    url = f"{_API_BASE}/bot{_token()}/{method}"
    async with httpx.AsyncClient(timeout=timeout) as cli:
        resp = await cli.post(url, json=payload)
    try:
        body = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError(f"Telegram {method} returned non-JSON: {resp.text[:200]}")
    if not body.get("ok"):
        # Surface Telegram's `description` so logs/UI carry the actual
        # rejection reason ("chat not found", "bot was blocked", etc.).
        raise RuntimeError(f"Telegram {method} failed: {body.get('description') or body}")
    return body.get("result") or {}


# ── Public API ───────────────────────────────────────────────────────────────
async def get_me() -> Dict[str, Any]:
    """Health check — returns the bot's own profile. Used by /status."""
    return await _request("getMe", {})


# Telegram's MarkdownV2 reserves a strict set of chars that must be
# escaped even inside otherwise-safe text. Rather than wrestle with that
# for every nudge, Phase 1 uses HTML mode which is more forgiving and
# matches the formatting the CoS actually emits (just <b> + <i>).
_HTML_ESCAPE_TABLE = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})


def escape_html(text: str) -> str:
    """Escape `< > &` only — safe for use inside `<b>` / `<i>` tags."""
    return (text or "").translate(_HTML_ESCAPE_TABLE)


async def send_message(
    chat_id: int | str,
    text: str,
    *,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = True,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Send a message to a connected Telegram chat. Returns the Telegram
    `Message` object (with `message_id`, `date`, etc.). Caller catches
    any RuntimeError to surface the failure to the user.
    """
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4096],  # Telegram caps single messages at 4096 chars
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _request("sendMessage", payload)


async def set_webhook(url: str, *, secret_token: Optional[str] = None) -> Dict[str, Any]:
    """Register `url` as the inbound webhook. Used at app startup in
    environments where the host is publicly HTTPS-reachable. Telegram
    rejects non-HTTPS URLs and a small set of host patterns; if your
    deployment can't host one (e.g. local dev), call `delete_webhook()`
    and switch to long-polling via `get_updates`.
    """
    payload: Dict[str, Any] = {
        "url": url,
        "allowed_updates": ["message", "callback_query"],
    }
    if secret_token:
        payload["secret_token"] = secret_token
    return await _request("setWebhook", payload)


async def delete_webhook() -> Dict[str, Any]:
    return await _request("deleteWebhook", {"drop_pending_updates": False})


async def get_webhook_info() -> Dict[str, Any]:
    return await _request("getWebhookInfo", {})


# ── Inbound update helpers ──────────────────────────────────────────────────
_NONCE_RE = re.compile(r"^/start(?:@\w+)?(?:\s+(?P<payload>\S+))?\s*$")


def parse_deep_link_payload(text: str) -> Optional[str]:
    """Extract the nonce (or other start payload) from a `/start <nonce>`
    message. Returns None if the message isn't a `/start` or has no
    payload — that's how Telegram tells us a returning user just opened
    the chat, vs. clicking a fresh deep link.
    """
    if not text:
        return None
    m = _NONCE_RE.match(text.strip())
    if not m:
        return None
    return m.group("payload") or None


def chat_id_from_update(update: Dict[str, Any]) -> Optional[int]:
    """Pull the `chat.id` out of any reasonable inbound update."""
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    if cid is None:
        cb = update.get("callback_query") or {}
        cid = ((cb.get("message") or {}).get("chat") or {}).get("id")
    try:
        return int(cid) if cid is not None else None
    except (ValueError, TypeError):
        return None


def text_from_update(update: Dict[str, Any]) -> Optional[str]:
    msg = update.get("message") or update.get("edited_message") or {}
    return msg.get("text")


def sender_from_update(update: Dict[str, Any]) -> Dict[str, Optional[str]]:
    msg = update.get("message") or update.get("edited_message") or {}
    sender = msg.get("from") or {}
    return {
        "id": str(sender.get("id") or "") or None,
        "username": sender.get("username"),
        "first_name": sender.get("first_name"),
        "last_name": sender.get("last_name"),
    }


# ── Convenience formatter ───────────────────────────────────────────────────
def format_nudge(*, org_label: str, body: str, action_label: Optional[str] = None) -> str:
    """Render a CoS nudge as Telegram HTML.

    Layout: `<b>[OrgName]</b> body text — optional action hint`
    Keeps the org tag at the front so multi-org users can instantly tell
    which workspace the alert is from.
    """
    org_part = f"<b>[{escape_html(org_label)}]</b>  " if org_label else ""
    body_part = escape_html(body)
    if action_label:
        body_part = f"{body_part}\n\n<i>{escape_html(action_label)}</i>"
    return f"{org_part}{body_part}"


async def safe_send(chat_id: int | str, text: str, **kwargs) -> bool:
    """Fire-and-forget send. Logs failures and returns success bool.
    Used by background pulses where one user blocking the bot
    shouldn't crash the whole sweep.
    """
    try:
        await send_message(chat_id, text, **kwargs)
        return True
    except Exception as exc:
        logger.warning("Telegram send to %s failed: %s", chat_id, exc)
        return False


# Bounded send semaphore so a big sweep never trips Telegram's
# 30-msg/sec global ceiling. We cap concurrency at 20 in-flight sends.
_SEND_SEM = asyncio.Semaphore(20)


async def send_throttled(chat_id: int | str, text: str, **kwargs) -> bool:
    async with _SEND_SEM:
        return await safe_send(chat_id, text, **kwargs)
