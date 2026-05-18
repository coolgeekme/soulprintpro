"""Phase-3 inbound pipeline for the Telegram Chief-of-Staff.

What this module does, end-to-end, for every inbound Telegram message:

  1. **Voice → text** (if `message.voice` or `message.audio` is set)
     Downloads the file from Telegram and transcribes it via Whisper
     using the Emergent LLM key. OGG/Opus is sent straight through —
     no ffmpeg conversion needed for Whisper API.

  2. **Org routing — 4-layer cascade** (`detect_org`)
       a. Slash command: "/org <name> ..."
       b. Inline fuzzy match against the user's org names
       c. Sticky context: last-routed org within 30 min
       d. Disambiguation prompt: numbered reply ("Reply 1, 2, ...")
     The pending-disambig state is persisted in
     `telegram_disambig_state` keyed by chat_id so the user's followup
     "1" or "2" is treated as a routing decision.

  3. **CoS turn** (`run_cos_turn`)
     Resolves the user's private Chief-of-Staff conversation for the
     routed org, appends the (text or transcript) message as if the
     user had typed it in the web UI, runs the agent's normal reply
     pipeline (which already has Composio tool access), and returns
     the agent's reply text to be sent back to Telegram.

The webhook in `telegram_routes.py` calls `handle_inbound` here;
`cos_pulse.handle_inbound_message` re-exports this for the existing
hook surface.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import httpx
from bson import ObjectId

import telegram_bot

logger = logging.getLogger("foundry.telegram_inbound")

# How confidently inline match has to score before we accept it. Pure
# substring hit always counts; fuzzy only if >= this ratio.
FUZZY_CUTOFF = 0.86


# ── Voice transcription ────────────────────────────────────────────────────
async def download_telegram_file(file_id: str) -> Tuple[bytes, str]:
    """Resolve `file_id` to bytes via Telegram's `getFile` + CDN URL.
    Returns `(audio_bytes, suggested_extension)`. Caller passes the
    extension to Whisper so the API can dispatch its codec parser.
    """
    info = await telegram_bot._request("getFile", {"file_id": file_id})
    path = info.get("file_path") or ""
    # Telegram's voice notes use .oga, music uses .mp3, video notes .mp4
    ext = (os.path.splitext(path)[1] or ".ogg").lstrip(".")
    url = f"https://api.telegram.org/file/bot{telegram_bot._token()}/{path}"
    async with httpx.AsyncClient(timeout=30) as cli:
        resp = await cli.get(url)
        resp.raise_for_status()
        return resp.content, ext


async def transcribe_voice(audio_bytes: bytes, ext: str) -> str:
    """Send audio to Whisper-1 via emergentintegrations + Emergent LLM
    key. Returns the recognised text. Raises on failure so the caller
    can decide whether to surface "couldn't hear that" to the user.
    """
    from emergentintegrations.llm.openai import OpenAISpeechToText
    key = os.environ.get("EMERGENT_LLM_KEY")
    if not key:
        raise RuntimeError("EMERGENT_LLM_KEY not configured.")
    # Whisper accepts: mp3, mp4, mpeg, mpga, m4a, wav, webm — and in
    # practice ogg/oga as well. Normalize Telegram's `.oga` → `.ogg`
    # since the latter is widely-accepted; if Whisper rejects we'd
    # need ffmpeg conversion as a follow-up.
    if ext.lower() in ("oga", "opus"):
        ext = "ogg"
    bio = BytesIO(audio_bytes)
    bio.name = f"voice.{ext}"
    stt = OpenAISpeechToText(api_key=key)
    res = await stt.transcribe(
        file=bio,
        model="whisper-1",
        response_format="json",
    )
    return (getattr(res, "text", None) or "").strip()


# ── Org routing cascade ────────────────────────────────────────────────────
_SLASH_ORG_RE = re.compile(r"^/org(?:@\w+)?\s+(?P<rest>.+)", re.DOTALL | re.IGNORECASE)


def _slash_org_parse(text: str, orgs: Optional[List[Dict[str, Any]]] = None) -> Optional[Tuple[str, str]]:
    """Match `/org Reggie Lab add Reggie to HubSpot`. Returns
    (org_name, body) on hit, None otherwise.

    Strategy: pull everything after the leading `/org` token, then —
    if `orgs` is supplied — try matching the LONGEST org name prefix
    against the user's actual org list. This lets users type natural
    multi-word org names like "/org Acme Corp draft a reply" without
    needing a `:` or `-` delimiter.

    Falls back to single-token name when no `orgs` list is provided
    (preserves backwards-compatible behaviour for callers that don't
    care about disambiguation).
    """
    m = _SLASH_ORG_RE.match((text or "").strip())
    if not m:
        return None
    rest = (m.group("rest") or "").strip()
    if not rest:
        return None
    if orgs:
        # Longest-org-name-prefix match. We sort the user's org names
        # by word count descending so multi-word names always beat
        # single-word ones when both prefix the message.
        cand_names = sorted(
            ((o.get("name") or "").strip() for o in orgs if o.get("name")),
            key=lambda n: -len(n.split()),
        )
        lc_rest = rest.lower()
        for name in cand_names:
            ln = name.lower()
            if lc_rest == ln:
                return name, ""
            # Match name followed by whitespace OR punctuation.
            if lc_rest.startswith(ln) and len(lc_rest) > len(ln) and not lc_rest[len(ln)].isalnum():
                body = rest[len(name):].lstrip(" :,-—\t\n").strip()
                return name, body
    # Fallback: single-word org name (legacy behaviour).
    parts = rest.split(None, 1)
    name = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    return name, body


def _fuzzy_score(needle: str, hay: str) -> float:
    return difflib.SequenceMatcher(a=needle.lower(), b=hay.lower()).ratio()


def _find_inline_org(text: str, orgs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Walk the user's orgs and look for an exact substring hit first;
    fall back to fuzzy match against any window of the message.

    Exact substring of an org name (case-insensitive) always wins.
    Fuzzy only fires if no exact hit and the best ratio >= FUZZY_CUTOFF.
    """
    lc = text.lower()
    # Exact pass — prefer the longest match so "AZ Pain Doctors" beats
    # "AZ" if both happen to be valid org names.
    exact = []
    for org in orgs:
        name = (org.get("name") or "").strip()
        if not name:
            continue
        if name.lower() in lc:
            exact.append((len(name), org))
    if exact:
        exact.sort(key=lambda t: -t[0])
        return exact[0][1]
    # Fuzzy: score each org name against any token window of the same
    # word count. Cheap heuristic, plenty good for org names < 5 words.
    tokens = text.split()
    best: Tuple[float, Optional[Dict[str, Any]]] = (0.0, None)
    for org in orgs:
        name = (org.get("name") or "").strip()
        if not name:
            continue
        n = len(name.split())
        for i in range(0, max(1, len(tokens) - n + 1)):
            window = " ".join(tokens[i:i + n])
            ratio = _fuzzy_score(name, window)
            if ratio > best[0]:
                best = (ratio, org)
    if best[0] >= FUZZY_CUTOFF:
        return best[1]
    return None


async def _user_orgs(db, user_id: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    async for m in db.org_members.find({"user_id": user_id}, {"_id": 0, "org_id": 1}):
        try:
            org = await db.organizations.find_one(
                {"_id": ObjectId(m["org_id"])}, {"_id": 1, "name": 1},
            )
        except Exception:
            continue
        if org:
            out.append({"id": str(org["_id"]), "name": org.get("name", "")})
    return out


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Mongo round-trips datetimes as offset-naive UTC. Re-stamp them
    as aware so timedelta arithmetic works without TypeError."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _read_pending_disambig(db, chat_id: int) -> Optional[Dict[str, Any]]:
    row = await db.telegram_disambig_state.find_one({"chat_id": chat_id})
    if not row:
        return None
    at = _aware(row.get("at"))
    if at is None or datetime.now(timezone.utc) - at > timedelta(minutes=5):
        # Stale prompt — clean it up so the next message doesn't get
        # misinterpreted as a disambig response.
        await db.telegram_disambig_state.delete_one({"chat_id": chat_id})
        return None
    return row


async def _write_pending_disambig(
    db, chat_id: int, message_text: str, candidates: List[Dict[str, Any]],
) -> None:
    await db.telegram_disambig_state.update_one(
        {"chat_id": chat_id},
        {"$set": {
            "chat_id": chat_id,
            "message_text": message_text,
            "candidates": [{"id": c["id"], "name": c["name"]} for c in candidates],
            "at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


async def _clear_pending_disambig(db, chat_id: int) -> None:
    await db.telegram_disambig_state.delete_one({"chat_id": chat_id})


async def detect_org(
    db, *, user_id: str, chat_id: int, text: str,
) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    """Run the routing cascade. Returns:
        (org | None, body, reason)
    Where:
        - org is the resolved {"id", "name"} or None if we should route
          to the Personal Concierge (org-agnostic default).
        - body is the message text minus any /org prefix (so the CoS
          doesn't see "/org Archeforge" as part of the prompt).
        - reason is a short string for the audit log.

    Routing philosophy (Feb 2026 redesign):
      The Telegram bot is **org-agnostic by default** — every message
      goes to the Personal Concierge unless the user EXPLICITLY routes
      it to a specific org via:
        a. ``/org <name> ...`` slash command
        b. naming an org inline in the message ("draft a reply for the
           Acme thread", "in Archeforge, …")
        c. answering a pending disambiguation numbered prompt
      We no longer auto-stick to the last-used org, and we no longer
      auto-route single-org users — both led to "wait, why did asking
      about my agenda end up logged under <random org>?" surprise.
    """
    orgs = await _user_orgs(db, user_id)
    if not orgs:
        # Personal Concierge still works without orgs (general chitchat).
        return None, text, "no_orgs"

    # ── Resolve a numbered reply against a pending disambiguation ─────
    pending = await _read_pending_disambig(db, chat_id)
    if pending and re.fullmatch(r"\s*(\d{1,2})\s*\.?\s*", text or ""):
        idx = int(re.findall(r"\d{1,2}", text)[0]) - 1
        cands = pending.get("candidates") or []
        if 0 <= idx < len(cands):
            picked = cands[idx]
            await _clear_pending_disambig(db, chat_id)
            return picked, pending.get("message_text") or "", "disambig_picked"
        # Out-of-range number — fall through to Personal Concierge.

    # ── Layer 1: slash command (the ONLY explicit org route) ──────────
    # We intentionally do NOT auto-route on inline mention of an org
    # name — users frequently ASK ABOUT an org in a meta question
    # ("what calendar is connected with DisciplinedOS?") and the
    # earlier inline-match heuristic would hijack those questions
    # into the org's CoS instead of letting the Personal Concierge
    # answer with cross-org context. To address a specific org's
    # CoS, users now must use `/org <name> ...` explicitly. The
    # Personal Concierge punts back with that instruction whenever a
    # request requires a write action on a specific org.
    parsed = _slash_org_parse(text or "", orgs=orgs)
    if parsed:
        name_q, body = parsed
        match = _find_inline_org(name_q, orgs)
        if match:
            return match, body, "slash"
        # Slash with unknown name — let user know but don't disambig.
        return None, text, "slash_unknown"

    # ── DEFAULT: Personal Concierge ───────────────────────────────────
    # No `/org` prefix → cross-org read-only assistant. The Concierge
    # handles calendar, inbox, doc search, status summaries, drafts,
    # and general chitchat. If the user wants to mutate org data, the
    # Concierge punts back with a `/org <name>` instruction.
    return None, text, "personal_default"


# ── CoS turn execution ─────────────────────────────────────────────────────
async def run_cos_turn(
    db, *, user_id: str, org_id: str, message_text: str,
) -> str:
    """Send `message_text` into the user's private Chief-of-Staff
    conversation for `org_id` and return the agent's reply text.

    Strategy:
      We do NOT replicate the full chat machinery here. Instead we POST
      a message into the existing CoS conversation via the same code
      path the web UI uses, then await the agent's reply.

    For Phase 3 we use a streamlined direct-call: load the CoS agent
    (`specialized-chief-of-staff`), run one LLM round with the org's
    org/user memory context. Full multi-turn tool-calling lands when
    we plumb this through the Composio agent loop.
    """
    # Lazy import avoids pulling all of server.py at module load.
    try:
        from server import telegram_cos_turn  # type: ignore
    except Exception as exc:
        logger.exception("telegram_cos_turn import failed")
        return f"⚠️ Couldn't reach the agent: {exc}"
    try:
        return await telegram_cos_turn(
            user_id=user_id,
            org_id=org_id,
            agent_id="specialized-chief-of-staff",
            message_text=message_text,
        )
    except Exception as exc:
        logger.exception("CoS turn failed")
        return f"⚠️ {exc}"


# ── Top-level inbound handler ──────────────────────────────────────────────
async def handle_inbound(
    db, *, chat_id: int, text: str, update: Dict[str, Any],
) -> None:
    """Called by the Telegram webhook. Orchestrates voice transcription,
    routing, the CoS turn, and the reply send.

    Errors are caught here and surfaced to the user as a short message
    so the chat never feels "dead".
    """
    user_doc = await db.users.find_one(
        {"telegram.chat_id": chat_id},
        {"_id": 1, "name": 1, "email": 1},
    )
    if not user_doc:
        await telegram_bot.safe_send(
            chat_id,
            "I don't recognise this chat. Open Foundry → Account → Telegram and tap Connect.",
        )
        return
    user_id = str(user_doc["_id"])

    # ── 1. Voice / audio transcription ─────────────────────────────────
    message = update.get("message") or update.get("edited_message") or {}
    voice = message.get("voice") or message.get("audio")
    transcript_used = False
    if voice and voice.get("file_id"):
        try:
            audio_bytes, ext = await download_telegram_file(voice["file_id"])
        except Exception as exc:
            logger.exception("Voice file download failed")
            await telegram_bot.safe_send(chat_id, f"⚠️ Couldn't fetch that voice note: {exc}")
            return
        # Send an "I heard…" hint immediately so the user sees feedback
        # before the Whisper round-trip + CoS turn. Telegram chats feel
        # dead without these heartbeats.
        try:
            text_from_voice = await transcribe_voice(audio_bytes, ext)
        except Exception as exc:
            logger.exception("Whisper transcription failed")
            await telegram_bot.safe_send(
                chat_id,
                f"⚠️ I downloaded your voice note but couldn't transcribe it: {exc}",
            )
            return
        if not text_from_voice:
            await telegram_bot.safe_send(chat_id, "🤷 I couldn't make out any words in that voice note. Try again?")
            return
        text = text_from_voice
        transcript_used = True
        # Echo the transcript so the user can see what I heard.
        await telegram_bot.safe_send(
            chat_id,
            telegram_bot.format_nudge(
                org_label="Foundry",
                body=f"🎙️ Heard: <i>{telegram_bot.escape_html(text)}</i>",
            ),
        )

    if not text:
        return

    # ── 2. Routing cascade ────────────────────────────────────────────
    org, body, reason = await detect_org(db, user_id=user_id, chat_id=chat_id, text=text)
    if org is None:
        if reason == "slash_unknown":
            await telegram_bot.safe_send(
                chat_id,
                "I couldn't find an org by that name. Try `/org <exact name> <message>` or just say the org name in your sentence.",
            )
            return
        # Default path: Personal Concierge. Handles cross-org calendar /
        # inbox / doc search / status summaries / general chitchat.
        # Punts back to a specific org's CoS only when a write action
        # is requested. See `personal_concierge.respond_personal`.
        org_label = "Foundry"  # branding for nudge wrapper
        try:
            from personal_concierge import respond_personal
            reply_text = await respond_personal(db, user_id=user_id, message=text)
        except Exception as exc:
            logger.exception("Personal Concierge failed")
            await telegram_bot.safe_send(
                chat_id,
                f"⚠️ Personal Concierge hit an error: {exc}",
            )
            return
        await db.telegram_inbound_log.insert_one({
            "chat_id": chat_id,
            "user_id": user_id,
            "org_id": None,
            "text": (text or "")[:2000],
            "voice": transcript_used,
            "routed_via": reason if reason in {"no_orgs"} else "personal_concierge",
            "reply_preview": (reply_text or "")[:400],
            "at": datetime.now(timezone.utc),
        })
        await _deliver_reply(
            chat_id=chat_id, org_label=org_label,
            reply_text=reply_text or "Done.",
            speak=transcript_used,
        )
        return

    # ── 3. Run the CoS turn for the routed org ────────────────────────
    reply_text = await run_cos_turn(
        db, user_id=user_id, org_id=org["id"], message_text=body or text,
    )

    # Log inbound for audit / debugging.
    await db.telegram_inbound_log.insert_one({
        "chat_id": chat_id,
        "user_id": user_id,
        "org_id": org["id"],
        "text": (text or "")[:2000],
        "voice": transcript_used,
        "routed_via": reason,
        "reply_preview": (reply_text or "")[:400],
        "at": datetime.now(timezone.utc),
    })

    await _deliver_reply(
        chat_id=chat_id, org_label=org["name"],
        reply_text=reply_text or "Done.",
        speak=transcript_used,
        user_id=user_id,
    )


async def _deliver_reply(
    *, chat_id: int, org_label: str, reply_text: str, speak: bool,
    user_id: Optional[str] = None,
) -> None:
    """Send the agent's reply back to the Telegram chat. Always sends
    the text version (so the user can scroll back and re-read).

    When `speak=True` (the user sent voice input) we ALSO synthesise a
    TTS voice note so they get an auditory reply that matches the input
    modality.

    When `user_id` is supplied we drain any pending Telegram photo
    attachments queued by ``server.telegram_cos_turn`` (i.e. images the
    agent generated via `[[IMAGE]]` blocks in the same turn) and send
    each as a native ``sendPhoto`` photo bubble.
    """
    formatted = telegram_bot.format_nudge(org_label=org_label, body=reply_text)
    await telegram_bot.safe_send(chat_id, formatted)

    # Photos queued by the CoS turn — drain & send before the voice note
    # so the visual context arrives BEFORE the spoken summary.
    if user_id:
        try:
            from server import pop_pending_telegram_photos  # type: ignore
            from pathlib import Path
            import os as _os
            queued = pop_pending_telegram_photos(user_id)
            upload_dir = Path(_os.environ.get("UPLOAD_DIR", "/app/backend/uploads"))
            for att in queued:
                fname = att.get("media_filename") or att.get("filename") or ""
                if not fname:
                    continue
                fpath = upload_dir / fname
                if not fpath.is_file():
                    # Best-effort fallback to the docs library copy.
                    docs_dir = Path(_os.environ.get("DOCS_DIR", "/app/backend/docs"))
                    fpath = docs_dir / fname
                if not fpath.is_file():
                    logger.warning("Queued Telegram photo missing on disk: %s", fname)
                    continue
                try:
                    img_bytes = fpath.read_bytes()
                except Exception as exc:
                    logger.warning("Could not read queued photo %s: %s", fname, exc)
                    continue
                caption = (att.get("title") or "") or None
                await telegram_bot.safe_send_photo(
                    chat_id, img_bytes, caption=caption, filename=fname,
                )
        except Exception as exc:
            logger.warning("Telegram photo delivery failed: %s", exc)

    if not speak:
        return
    try:
        from forge_tts import tts_to_opus
        audio = await tts_to_opus(reply_text)
    except Exception as exc:
        logger.warning("TTS reply generation failed: %s", exc)
        return
    if not audio:
        return
    await telegram_bot.safe_send_voice(chat_id, audio)
