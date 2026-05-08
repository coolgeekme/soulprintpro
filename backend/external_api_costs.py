"""External API cost tracking — every non-LLM third-party call.

The existing `llm_logging.py` tracks every call to OpenAI / Anthropic /
Gemini and persists `cost_usd` per call. But missions and audits also
hit a half-dozen other paid APIs that go untracked:

  • Brave Search        — $5 per 1,000 search calls (~$0.005/call)
  • Perplexity Sonar Pro — $3/M input + $15/M output + $0.010/call (medium ctx)
  • kie.ai (Suno / Veo)  — credit-based, ~$0.05/song, ~$0.05/sec video
  • Resend emails        — $0.0001/email after free tier
  • Firebase Auth        — free up to 50k MAU, then $0.0055/MAU

This module gives us:
  1. ONE rate table (`EXTERNAL_API_RATES`) so pricing changes land in
     one place.
  2. ONE logger function (`log_external_call`) that writes a row to the
     `external_api_calls` collection with the same shape as `llm_calls`
     (provider, kind_label, cost_usd, org_id, mission_id, etc.) so the
     admin dashboard can aggregate both collections together.
  3. Best-effort + non-blocking — any failure inside the logger MUST
     NOT take down the calling code path. We log a debug message and
     return a no-op.

Why a separate collection (vs. mixing into `llm_calls`):
  - Different pricing models (per-call, per-second, per-email) mean the
    raw fields differ enough that a single schema gets messy.
  - Keeping LLM spend pure makes it easy to show "AI brain spend" vs
    "infrastructure spend" — useful for org budget conversations.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from llm_logging import _CALL_CTX  # reuse same contextvars (org/mission/user)

logger = logging.getLogger(__name__)

# Lazy global db handle — set at app startup via `set_db(db)` so the
# loggers don't have to thread `db` through every wrapper call site
# (brave_search, perplexity_client, kie_client, email_service all
# benefit). Same pattern as `browser_tools.set_persistence`.
_DB = None


def set_db(db) -> None:
    """Register the Mongo db handle so `log_external_call_fire_and_forget`
    can persist rows without each caller passing it."""
    global _DB
    _DB = db


# Canonical USD rate table for every external API we hit. Update when
# providers change pricing — single source of truth.
# Last verified: Feb 2026.
EXTERNAL_API_RATES: dict[str, dict[str, float]] = {
    # ── Search APIs ─────────────────────────────────────────────────
    # Brave Search Web — flat $5/1000 calls = $0.005/call
    "brave.search":          {"per_call": 0.005},
    # Perplexity Sonar Pro: $3/M input + $15/M output + per-request fee
    # by context. We default to "medium" context which most of our
    # missions use → $10/1000 = $0.010/call. For exact tracking we ALSO
    # capture token counts and bill them on top.
    "perplexity.sonar-pro":  {"input": 0.003, "output": 0.015, "per_call": 0.010},
    "perplexity.sonar":      {"input": 0.001, "output": 0.001, "per_call": 0.005},

    # ── Email ──────────────────────────────────────────────────────
    # Resend: free up to 3000/mo, then $0.0001/email. We bill at
    # $0.0001 from the first email so the dashboard reflects "what
    # this would cost outside the free tier" — gives a true cost view
    # at scale.
    "resend.email":          {"per_call": 0.0001},

    # ── Music / Audio (kie.ai Suno) ─────────────────────────────────
    # Suno V4 / V5 via kie.ai — ~$0.05/song. Charged per generation
    # request, regardless of song length. Default rate; can be
    # overridden per-call if kie.ai's billing changes.
    "kie.suno":              {"per_call": 0.05},

    # ── Video (kie.ai Veo / Sora / etc.) ────────────────────────────
    # Veo 3.1 fast: $0.05/second of generated video. We log seconds
    # so the dashboard can multiply correctly.
    "kie.veo3_fast":         {"per_second": 0.05},
    "kie.veo3":              {"per_second": 0.40},
    "kie.veo3_lite":         {"per_second": 0.05},
    # Sora 2 via kie.ai
    "kie.sora-2":            {"per_second": 0.30},
    # Kling family (kie.ai)
    "kie.kling-3":           {"per_second": 0.15},
    "kie.kling-3-pro":       {"per_second": 0.30},
    # Other generic kie.ai video models — fall through to this
    "kie.video.default":     {"per_second": 0.10},
    # Image generation
    "kie.image.default":     {"per_call": 0.03},

    # ── Auth / Hosting (mostly free for our scale) ─────────────────
    # Firebase Auth: free up to 50k MAU, then $0.0055/MAU. We don't
    # track these per-call because they only bill at month boundaries.
    # (Listed for completeness so the admin dashboard can show $0.00.)
    "firebase.auth":         {"per_call": 0.0},
}

# When provider is unknown we log it under this rate so we never lose
# the call entirely (better $0.00 than a missing audit trail).
_DEFAULT_RATE = {"per_call": 0.0}


def estimate_external_cost(
    *, provider: str,
    calls: int = 1,
    seconds: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> float:
    """Compute the cost of one external API call.

    Multiple cost dimensions stack — e.g. a Perplexity Sonar Pro call
    bills tokens AND a per-request fee. The unused dimensions are
    safely 0 and contribute nothing.
    """
    rate = EXTERNAL_API_RATES.get(provider, _DEFAULT_RATE)
    cost = 0.0
    if "per_call" in rate:
        cost += float(rate["per_call"]) * calls
    if "per_second" in rate and seconds > 0:
        cost += float(rate["per_second"]) * float(seconds)
    if "input" in rate and input_tokens > 0:
        cost += (input_tokens / 1000.0) * float(rate["input"])
    if "output" in rate and output_tokens > 0:
        cost += (output_tokens / 1000.0) * float(rate["output"])
    return max(0.0, cost)


async def log_external_call(
    db, *,
    provider: str,                    # "brave.search", "kie.suno", etc.
    kind_label: str = "",             # "audit_research", "social_post", etc.
    calls: int = 1,
    seconds: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    success: bool = True,
    error: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Persist one external API call to MongoDB. Best-effort + silent.

    Pulls org/mission/user/conversation IDs from the current
    contextvars (set by `with_call_context` higher in the request
    handler) so the admin dashboard can attribute spend correctly.
    """
    if db is None:
        return
    cost = estimate_external_cost(
        provider=provider,
        calls=calls, seconds=seconds,
        input_tokens=input_tokens, output_tokens=output_tokens,
    )
    ctx = _CALL_CTX.get() or {}
    doc = {
        "provider": provider,
        "kind_label": kind_label or "",
        "calls": int(calls),
        "seconds": float(seconds),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cost_usd": float(cost),
        "success": bool(success),
        "error": (error or "")[:500] if error else "",
        "org_id":          ctx.get("org_id") or "",
        "user_id":         ctx.get("user_id") or "",
        "mission_id":      ctx.get("mission_id") or "",
        "conversation_id": ctx.get("conversation_id") or "",
        "agent_id":        ctx.get("agent_id") or "",
        "ts": datetime.now(timezone.utc),
    }
    if extra and isinstance(extra, dict):
        # Limit extra payload size so a runaway caller can't bloat
        # the collection. 4KB is plenty for short summaries.
        try:
            doc["extra"] = {k: str(v)[:512] for k, v in list(extra.items())[:20]}
        except Exception:
            pass
    try:
        await db.external_api_calls.insert_one(doc)
    except Exception as exc:
        logger.debug("external API log failed for %s: %s", provider, exc)


async def ensure_indexes(db) -> None:
    """Idempotent index creation. Called once on app startup."""
    try:
        await db.external_api_calls.create_index("ts")
        await db.external_api_calls.create_index([("org_id", 1), ("ts", -1)])
        await db.external_api_calls.create_index([("provider", 1), ("ts", -1)])
        await db.external_api_calls.create_index([("kind_label", 1), ("ts", -1)])
    except Exception as exc:
        logger.warning("external_api_calls index creation skipped: %s", exc)


__all__ = [
    "EXTERNAL_API_RATES",
    "estimate_external_cost",
    "log_external_call",
    "log_external_call_fire_and_forget",
    "set_db",
    "ensure_indexes",
]


def log_external_call_fire_and_forget(**kwargs) -> None:
    """Schedule `log_external_call` on the running event loop without
    awaiting it. Use this from sync wrapper code paths or anywhere
    blocking on a Mongo write would hurt latency.

    Silently no-ops if no event loop is running OR `set_db` was never
    called (e.g. during unit tests or a script invocation).
    """
    if _DB is None:
        return
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop and loop.is_running():
            asyncio.create_task(log_external_call(_DB, **kwargs))
    except Exception:  # pragma: no cover
        pass
