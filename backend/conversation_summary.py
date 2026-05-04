"""conversation_summary.py — Rolling AI summary for long chat threads.

The Chief of Staff chat (and any long conversation) tends to blur topics
together when it grows past ~30 messages. Rather than force the user to
manually archive / restart, we quietly generate a 2-4 sentence AI summary
that covers the "earlier" block of messages so users see a clean recap
on top and only the recent N messages below.

How it's wired:
  • ``GET /api/conversations/{id}/summary`` — returns the cached summary
    that covers messages 0..(total - KEEP_RECENT). If the cache is stale
    (new messages arrived past the threshold) we silently re-summarise
    and update the cache. If total messages <= KEEP_RECENT, returns
    ``{summary_available: false}`` so the frontend can skip rendering.

  • Cache keyed by ``(conversation_id, covers_up_to_message_id)`` so
    re-opening a conversation doesn't re-hit the LLM.

  • Threshold: we only summarise when there are at least ``MIN_EARLIER``
    messages beyond the kept window — otherwise the summary would be
    weaker than the raw messages.

Cost: ~200-400 input tokens per summary, ~100 output. Called at most
once per conversation-open + once per 20 new messages. Negligible.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)

# How many most-recent messages stay visible verbatim. Older messages
# are folded into the AI summary.
KEEP_RECENT = 20
# Only bother summarising when there's at least this many earlier messages
# (otherwise the summary is lower-value than the raw thread).
MIN_EARLIER = 10


def _fmt_msg(m: dict) -> str:
    """Single-line rendering of a message for the summariser prompt.
    Keeps speaker identity + a reasonable text length so the model has
    enough signal to extract topics / decisions / action items."""
    who = m.get("sender_name") or m.get("agent_id") or m.get("sender_type", "user")
    text = (m.get("text") or "").strip()
    if not text and m.get("attachments"):
        text = f"[{len(m['attachments'])} file(s) attached]"
    # Cap each line — 600 chars is plenty to carry a paragraph-level idea.
    if len(text) > 600:
        text = text[:597] + "…"
    return f"{who}: {text}"


async def _generate_summary(
    llm_chat_cls, user_message_cls, emergent_key: str,
    org_name: str, conv_title: str, messages: list[dict],
) -> str:
    """Ask gpt-4o-mini for a tight 2-4 sentence recap of ``messages``.
    Returns the summary text. Any failure is logged and we return an
    empty string so the endpoint falls back gracefully.
    """
    if not messages:
        return ""
    body_lines = [_fmt_msg(m) for m in messages]
    body = "\n".join(body_lines)
    # Hard cap the prompt size — if the earlier block is huge we take
    # the first 40 and the last 40 messages (bookends carry the most
    # signal: what started the thread + what the team most recently did).
    if len(body_lines) > 80:
        head = "\n".join(body_lines[:40])
        tail = "\n".join(body_lines[-40:])
        body = f"{head}\n\n[… {len(body_lines) - 80} messages elided …]\n\n{tail}"
    system = (
        f"You are the Chief of Staff summariser for the {org_name} organisation. "
        f"Given the earlier portion of a chat thread, produce a concise 2-4 sentence "
        f"recap that captures: the main topic(s), any decisions reached, and any "
        f"open questions or action items. Write in third person, present tense. "
        f"No preamble, no headings, no lists — just flowing prose. Treat this as "
        f"what a busy owner would skim before diving into the most recent messages."
    )
    try:
        chat = llm_chat_cls(
            api_key=emergent_key,
            session_id=f"convsummary-{id(messages)}",
            system_message=system,
        ).with_model("openai", "gpt-4o-mini")
        reply = await chat.send_message(user_message_cls(
            text=f"Conversation: {conv_title or 'Chief of Staff chat'}\n\n{body}\n\n"
                 f"Write the 2-4 sentence recap now."
        ))
        return (reply or "").strip()
    except Exception as exc:
        logger.warning("Conversation summary LLM call failed: %s", exc)
        return ""


def make_summary_router(*, db, llm_chat_cls, user_message_cls,
                        emergent_key: str, get_current_user,
                        load_conv_or_403):
    r = APIRouter(prefix="/api")

    @r.get("/conversations/{conv_id}/summary")
    async def get_summary(conv_id: str, u: dict = Depends(get_current_user)):
        conv = await load_conv_or_403(conv_id, u["id"])
        # Count every message in the conversation. We only summarise the
        # "earlier" block — i.e. everything older than the last KEEP_RECENT.
        total = await db.messages.count_documents({"conversation_id": conv_id})
        earlier_count = max(0, total - KEEP_RECENT)
        if earlier_count < MIN_EARLIER:
            return {"summary_available": False, "total": total,
                    "earlier_count": earlier_count,
                    "keep_recent": KEEP_RECENT}

        # Identify the last earlier message — that's our cache key.
        # (Messages land in insertion order; `timestamp` is our sort key.)
        earlier_msgs = await db.messages.find(
            {"conversation_id": conv_id}, {"_id": 0}
        ).sort("timestamp", 1).limit(earlier_count).to_list(earlier_count)
        if not earlier_msgs:
            return {"summary_available": False, "total": total,
                    "earlier_count": earlier_count,
                    "keep_recent": KEEP_RECENT}
        last_earlier_id = earlier_msgs[-1].get("id") or ""

        # Cache hit?
        cached = await db.conversation_summaries.find_one(
            {"conversation_id": conv_id, "covers_up_to": last_earlier_id},
            {"_id": 0},
        )
        if cached and cached.get("text"):
            return {
                "summary_available": True,
                "text": cached["text"],
                "covers_count": cached.get("covers_count", earlier_count),
                "covers_up_to": last_earlier_id,
                "keep_recent": KEEP_RECENT,
                "generated_at": cached.get("generated_at"),
                "cached": True,
                "total": total,
            }

        # Need to generate. Pull org name for flavour.
        org_name = ""
        try:
            from bson import ObjectId
            team = await db.teams.find_one({"_id": ObjectId(conv.get("team_id"))})
            if team:
                org = await db.organizations.find_one({"_id": ObjectId(team["org_id"])})
                if org:
                    org_name = org.get("name") or ""
        except Exception:
            pass
        text = await _generate_summary(
            llm_chat_cls, user_message_cls, emergent_key,
            org_name=org_name, conv_title=conv.get("title", ""),
            messages=earlier_msgs,
        )
        if not text:
            return {"summary_available": False, "total": total,
                    "earlier_count": earlier_count,
                    "keep_recent": KEEP_RECENT}

        doc = {
            "conversation_id": conv_id,
            "covers_up_to": last_earlier_id,
            "covers_count": earlier_count,
            "text": text,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await db.conversation_summaries.update_one(
                {"conversation_id": conv_id, "covers_up_to": last_earlier_id},
                {"$set": doc},
                upsert=True,
            )
        except Exception as exc:
            logger.warning("Failed to cache summary: %s", exc)
        return {
            "summary_available": True,
            "text": text,
            "covers_count": earlier_count,
            "covers_up_to": last_earlier_id,
            "keep_recent": KEEP_RECENT,
            "generated_at": doc["generated_at"],
            "cached": False,
            "total": total,
        }

    return r
