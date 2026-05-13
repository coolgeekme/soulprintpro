"""User-scoped memory store — Phase 1.

WHAT THIS IS
============
A lightweight long-term memory layer that watches each agent reply,
asks an LLM to extract durable facts about the user from the user→agent
exchange, and persists them so future replies can be primed with what
we already know.

This is Phase 1 of the memory roadmap. Scope is **user-only** for now,
but the data model includes `scope` / `org_id` / `team_id` from day one
so Phase 2 (team + org + shared rings, promotion flow) is a clean
extension rather than a schema migration.

CREDIT
======
The extraction prompt pattern + categorisation + importance scoring +
dedupe heuristics are adapted from the open-source SoulPrint Memory
SDK (https://github.com/coolgeekme/soulprint-memory-sdk). We are not
importing the JS package — Foundry's backend is Python and our
multi-tenant access-control model has to live in our own DB layer.
What we re-use is the *extraction approach*, not the storage layer.

KEY DESIGN DECISIONS
====================
• Extraction runs as a **background task** after the agent reply is
  already saved + returned to the user. The user never waits on it.
• Categories are agency-relevant, not consumer-companion ones:
  brand-voice / pricing / icp / past-clients / playbooks /
  agent-feedback / preferences / work / other.
• Dedupe is `(user_id, scope, content_lower)` — a unique index prevents
  near-duplicates from accumulating across long conversations.
• At injection time we cap the prompt budget at ~800 tokens of memory
  context, ordered by importance > recency.
• Manual edits / deletes are first-class — the user owns their memory
  and can curate it from the Profile → Memory panel.

DATA MODEL  (collection: `user_memories`)
-----------------------------------------
{
    id:          uuid str
    user_id:     str
    scope:       "user"  (Phase 1 only; Phase 2 adds "team"/"org"/"shared")
    org_id:      str | None  (always populated when extracted from a conv)
    team_id:     str | None  (always populated when extracted from a conv)
    content:     str (single durable fact, 1 sentence)
    category:    str  (one of CATEGORIES)
    importance:  "low" | "medium" | "high"
    source:      "extracted" | "manual"
    source_conv_id: str | None
    source_msg_id:  str | None
    created_at:  datetime (UTC)
    updated_at:  datetime (UTC)
    is_archived: bool  (soft-delete; we never hard-delete extracted
                       memories so the audit log stays intact, but
                       users can mark "do not surface")
}
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Category vocabulary ────────────────────────────────────────────────
# Picked to be agency-relevant — what an account manager would actually
# want their AI assistant to remember about a client.
CATEGORIES = (
    "brand-voice",      # the org's tone, taboo words, signature phrases
    "pricing",          # tier preferences, budget sensitivities
    "icp",              # ideal customer profile, target segments
    "past-clients",     # named clients they've served + outcomes
    "playbooks",        # recurring missions / workflows they like
    "agent-feedback",   # which agents they prefer for which jobs
    "preferences",      # output style, format preferences, "always do X"
    "work",             # role, projects, day-to-day responsibilities
    "other",            # catch-all so the LLM doesn't force a bad fit
)

IMPORTANCE_VALUES = ("low", "medium", "high")
_IMPORTANCE_WEIGHT = {"high": 3, "medium": 2, "low": 1}


# ── Extraction prompt ──────────────────────────────────────────────────
# Adapted from the SoulPrint Memory SDK's prompt pattern. Returns
# strict JSON we can parse without regex.
_EXTRACT_SYSTEM = """You extract durable, agency-relevant facts about a single user from a recent message exchange.

You return STRICT JSON — no prose, no markdown fences, no commentary. The shape is:

{
  "memories": [
    {
      "content":    "<one durable fact, 1 short sentence, in third person about the user>",
      "category":   "<one of: brand-voice, pricing, icp, past-clients, playbooks, agent-feedback, preferences, work, other>",
      "importance": "<low | medium | high>"
    }
  ]
}

RULES
-----
1. Only extract DURABLE facts — things that will still be true in 30 days. Not transient state ("I'm tired today", "asked about pricing once").
2. Only extract things ABOUT THE USER — not about the world or third parties. "Foundry has 3 tiers" is NOT a memory; "User runs a 5-person agency in Austin" IS.
3. Extract NOTHING if the exchange contains no durable user-facts. Return `{"memories": []}`.
4. Deduplicate within the same response — never return two facts that say the same thing.
5. Importance: "high" = identity-defining (role, business, family, hard constraint). "medium" = stable preference. "low" = mild signal.
6. Write each `content` in CONCISE third-person about the user. Examples:
     - "User leads a small B2B SaaS marketing team focused on healthcare clients."
     - "User prefers Andy Crestodina-style website framings (clarity over cleverness)."
     - "User has had bad experiences with copy that uses 'unleash' / 'unlock' / 'supercharge'."
7. NEVER fabricate. If you're uncertain, omit the memory.
8. NEVER include passwords, full credit-card numbers, API keys, or anything that looks like a credential, even if the user pasted one.
9. Maximum 5 memories per extraction call.
"""


# ── Public API ─────────────────────────────────────────────────────────

async def ensure_indexes(db) -> None:
    """Create the index set we rely on. Idempotent — safe to call on
    every backend boot."""
    await db.user_memories.create_index(
        [("user_id", 1), ("scope", 1), ("is_archived", 1)],
        name="user_scope_archived_idx",
    )
    # Dedupe key: same content for the same (user, scope) — case-
    # insensitive via the lowercased `content_key` we maintain.
    await db.user_memories.create_index(
        [("user_id", 1), ("scope", 1), ("content_key", 1)],
        unique=True,
        name="user_scope_content_unique_idx",
    )


def _content_key(content: str) -> str:
    """Normalize so dedupe catches "User likes X." == "user likes x"."""
    return re.sub(r"\s+", " ", (content or "").strip().lower())


async def list_memories(
    db, *, user_id: str, scope: str = "user",
    include_archived: bool = False, limit: int = 200,
) -> List[Dict[str, Any]]:
    """Return memories for a user, newest first."""
    q: Dict[str, Any] = {"user_id": user_id, "scope": scope}
    if not include_archived:
        q["is_archived"] = {"$ne": True}
    cur = db.user_memories.find(q, {"_id": 0}).sort("created_at", -1).limit(limit)
    return [m async for m in cur]


async def add_memory(
    db, *, user_id: str, content: str,
    category: str = "other", importance: str = "medium",
    source: str = "manual",
    org_id: Optional[str] = None, team_id: Optional[str] = None,
    source_conv_id: Optional[str] = None,
    source_msg_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Insert a single memory. Returns the row, or None if it was a
    duplicate (we silently skip rather than raise so background
    extraction can fire-and-forget)."""
    content = (content or "").strip()
    if not content:
        return None
    if category not in CATEGORIES:
        category = "other"
    if importance not in IMPORTANCE_VALUES:
        importance = "medium"
    now = datetime.now(timezone.utc)
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "scope": "user",  # Phase 1 only
        "org_id": org_id,
        "team_id": team_id,
        "content": content,
        "content_key": _content_key(content),
        "category": category,
        "importance": importance,
        "source": source,
        "source_conv_id": source_conv_id,
        "source_msg_id": source_msg_id,
        "created_at": now,
        "updated_at": now,
        "is_archived": False,
    }
    try:
        await db.user_memories.insert_one(doc)
    except Exception as exc:
        # Most likely a duplicate-key collision — perfectly fine, our
        # dedupe key did its job. Log other errors at info.
        if "duplicate key" not in str(exc).lower():
            logger.info("add_memory dropped: %s", exc)
        return None
    return {k: v for k, v in doc.items() if k != "_id" and k != "content_key"}


async def update_memory(
    db, *, memory_id: str, user_id: str,
    content: Optional[str] = None,
    category: Optional[str] = None,
    importance: Optional[str] = None,
    is_archived: Optional[bool] = None,
) -> bool:
    """Edit a memory. Users own their memories; the user_id is part of
    the filter so one user can't modify another's row."""
    patch: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if content is not None:
        c = content.strip()
        if not c:
            return False
        patch["content"] = c
        patch["content_key"] = _content_key(c)
    if category is not None and category in CATEGORIES:
        patch["category"] = category
    if importance is not None and importance in IMPORTANCE_VALUES:
        patch["importance"] = importance
    if is_archived is not None:
        patch["is_archived"] = bool(is_archived)
    res = await db.user_memories.update_one(
        {"id": memory_id, "user_id": user_id},
        {"$set": patch},
    )
    return res.modified_count > 0


async def delete_memory(db, *, memory_id: str, user_id: str) -> bool:
    """Hard-delete. We expose archive as a soft-delete in the UI; this
    is the explicit "purge" action for users who want a memory gone."""
    res = await db.user_memories.delete_one(
        {"id": memory_id, "user_id": user_id},
    )
    return res.deleted_count > 0


async def build_memory_context_block(
    db, *, user_id: str, max_chars: int = 2400,
) -> str:
    """Return a markdown block to inject into the agent's system prompt.

    Empty string when the user has no memories — caller can append
    unconditionally without polluting the prompt with empty headers.
    """
    rows = await list_memories(db, user_id=user_id, scope="user", limit=80)
    if not rows:
        return ""
    # Sort by importance weight desc, then recency desc.
    rows.sort(
        key=lambda r: (
            _IMPORTANCE_WEIGHT.get(r.get("importance"), 0),
            r.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    bucketed: Dict[str, List[str]] = {}
    total = 0
    for r in rows:
        cat = r.get("category") or "other"
        line = f"- {r['content']}"
        if total + len(line) > max_chars:
            break
        bucketed.setdefault(cat, []).append(line)
        total += len(line) + 1

    if not bucketed:
        return ""

    lines = [
        "\nWHAT YOU REMEMBER ABOUT THIS USER (from past conversations).",
        "Use this context naturally — never recite it back at them. "
        "Do not say 'I remember that you…' unless they ask. Just let it "
        "inform tone, recommendations, and the level of detail you give:",
    ]
    for cat in CATEGORIES:
        items = bucketed.get(cat)
        if items:
            lines.append(f"\n  [{cat}]")
            lines.extend(f"  {ln}" for ln in items)
    return "\n".join(lines)


# ── LLM-backed extraction ───────────────────────────────────────────────

_MAX_INPUT_CHARS = 4000


async def extract_and_store(
    db, *, llm_chat_cls, user_message_cls, emergent_llm_key: str,
    user_id: str, user_message: str, agent_message: str,
    org_id: Optional[str] = None, team_id: Optional[str] = None,
    conv_id: Optional[str] = None, msg_id: Optional[str] = None,
) -> int:
    """Extract durable facts about the user from a (user, agent) turn
    pair and store any new ones. Returns the number stored.

    Designed to be called from a fire-and-forget asyncio.create_task —
    failures here MUST NOT bubble up to the conversation endpoint.
    """
    if not user_message or not user_message.strip():
        return 0
    payload = json.dumps({
        "user_message": (user_message or "")[:_MAX_INPUT_CHARS],
        "agent_message": (agent_message or "")[:_MAX_INPUT_CHARS],
    }, ensure_ascii=False)
    try:
        # gpt-5.2 nano-class extraction call — small, cheap, fast.
        # We reuse Foundry's emergent LLM gateway via the same llm_chat
        # class the rest of the codebase uses.
        chat = llm_chat_cls(
            api_key=emergent_llm_key,
            session_id=f"memory-extract-{user_id}",
            system_message=_EXTRACT_SYSTEM,
        ).with_model("openai", "gpt-5.2-mini")
        msg = user_message_cls(text=payload)
        raw = await chat.send_message(msg)
    except Exception as exc:
        logger.info("memory extraction LLM call failed: %s", exc)
        return 0
    text = (raw or "").strip()
    if text.startswith("```"):
        # Defensive in case the model still emitted a fence
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.info("memory extraction returned non-JSON: %s", text[:200])
        return 0
    items = data.get("memories") or []
    if not isinstance(items, list):
        return 0
    stored = 0
    for it in items[:5]:
        if not isinstance(it, dict):
            continue
        content = (it.get("content") or "").strip()
        category = it.get("category") or "other"
        importance = it.get("importance") or "medium"
        row = await add_memory(
            db,
            user_id=user_id,
            content=content,
            category=category,
            importance=importance,
            source="extracted",
            org_id=org_id,
            team_id=team_id,
            source_conv_id=conv_id,
            source_msg_id=msg_id,
        )
        if row:
            stored += 1
    if stored:
        logger.info(
            "memory: stored %d new fact(s) for user=%s conv=%s",
            stored, user_id, conv_id,
        )
    return stored


def schedule_extraction(coro) -> None:
    """Fire-and-forget wrapper: drops the future and swallows errors so
    a slow / failing extraction can never affect the conversation
    endpoint that scheduled it."""
    async def _wrapped():
        try:
            await coro
        except Exception as exc:
            logger.info("background memory extraction failed: %s", exc)
    asyncio.create_task(_wrapped())
