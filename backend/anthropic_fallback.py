"""anthropic_fallback.py — Direct Anthropic SDK fallback for Claude calls.

Architecture:
  • All LLM calls in this app primarily route through ``emergentintegrations.llm.chat``
    (LlmChat + UserMessage) using the EMERGENT_LLM_KEY. That's the cheapest,
    unified-billing path.
  • If a Claude-flavoured call (any model id starting with ``claude``) raises a
    transient error (rate limit, timeout, 5xx, network drop), we get a single
    retry chance against the *direct* Anthropic API using ANTHROPIC_API_KEY.
    This gives the app a safety net without any visible behaviour change.

Design rules:
  • Async only. All FastAPI handlers in this codebase are async, and the
    Anthropic SDK exposes ``AsyncAnthropic`` for the same pattern.
  • Single retry. We don't loop — just one direct-API attempt. If that also
    fails the original exception is re-raised so callers handle it the same
    way they did before this fallback existed.
  • Permanent errors (auth 401, malformed-request 400, 404) bypass the
    fallback entirely. Retrying those wastes quota and prolongs failure.
  • Cost / token telemetry on the fallback path is intentionally skipped —
    the admin dashboard is keyed off the Emergent telemetry collection. We
    still log a structured INFO line each time the fallback fires so we can
    audit how often it's being relied on.

Usage from the rest of the app:

    from anthropic_fallback import send_with_fallback
    reply = await send_with_fallback(
        chat, user_message, model="claude-sonnet-4-5",
        system_prompt=system_msg, prompt_text=user_text,
    )

The first two args are the live ``LlmChat`` + ``UserMessage`` already
prepared by the caller. ``system_prompt`` + ``prompt_text`` are only used
if we have to reconstruct the request for the direct Anthropic call.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


# Anthropic only — anything not starting with this prefix bypasses the
# fallback path entirely (OpenAI / Gemini failures don't have a direct
# parallel here, and Anthropic doesn't host those models).
_CLAUDE_PREFIX = "claude"

# The Emergent SDK wraps several backend HTTP libs and surfaces errors as
# generic exceptions. We pattern-match on the string to decide whether it's
# transient — same approach the rest of the codebase uses for retry logic
# (see e.g. perplexity_client.py). Cheap, robust, no SDK-coupling.
_TRANSIENT_MARKERS = (
    "429",                  # rate limit
    "timeout", "timed out",
    "500", "502", "503", "504",
    "connection", "remoteprotocolerror", "remote disconnected",
    "overloaded", "service unavailable",
)

_PERMANENT_MARKERS = (
    "401", "unauthorized", "invalid api key", "authentication",
    "400", "invalid request", "missing required",
    "404", "not found",
)


def _is_transient(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if any(m in msg for m in _PERMANENT_MARKERS):
        return False
    return any(m in msg for m in _TRANSIENT_MARKERS)


def _is_claude_model(model_id: str) -> bool:
    return (model_id or "").lower().startswith(_CLAUDE_PREFIX)


async def _direct_anthropic_call(
    *, model: str, system_prompt: str, prompt_text: str,
    max_tokens: int = 4096, temperature: float = 0.7,
) -> str:
    """One async call straight to Anthropic. Returns the assistant text.
    Lazy-imports ``AsyncAnthropic`` so this module loads even when the SDK
    isn't installed — only the actual fallback invocation requires it."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set — direct fallback unavailable."
        )
    from anthropic import AsyncAnthropic  # local import keeps cold-start fast
    client = AsyncAnthropic(api_key=api_key)
    try:
        message = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt or "",
            messages=[{"role": "user", "content": prompt_text or ""}],
        )
        # The SDK returns a list of content blocks; we take the first text
        # block. Edge cases (empty list, non-text block) raise so the caller
        # falls back to the original exception.
        if not message.content:
            raise RuntimeError("Anthropic returned empty content")
        block = message.content[0]
        text = getattr(block, "text", "") or ""
        if not text:
            raise RuntimeError("Anthropic returned content without text")
        return text
    finally:
        # AsyncAnthropic uses an internal httpx client — closing it ensures
        # we don't leak sockets on the busy fallback path.
        try:
            await client.close()
        except Exception:
            pass


async def send_with_fallback(
    chat, user_message, *, model: str,
    system_prompt: str = "", prompt_text: str = "",
    max_tokens: int = 4096, temperature: float = 0.7,
) -> str:
    """Drop-in wrapper for ``await chat.send_message(user_message)``.

    On transient failure for a Claude model, retries ONCE against direct
    Anthropic. Returns the reply text either way. Re-raises the original
    error if the model isn't Claude, the error isn't transient, or the
    fallback also fails.
    """
    try:
        return await chat.send_message(user_message)
    except Exception as primary_exc:
        if not _is_claude_model(model):
            raise
        if not _is_transient(primary_exc):
            raise
        logger.info(
            "Emergent Claude call failed transiently — attempting direct "
            "Anthropic fallback. model=%s err=%s",
            model, primary_exc,
        )
        try:
            return await _direct_anthropic_call(
                model=model,
                system_prompt=system_prompt,
                prompt_text=prompt_text,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as fb_exc:
            logger.warning(
                "Direct Anthropic fallback also failed. primary=%s fallback=%s",
                primary_exc, fb_exc,
            )
            # Re-raise the ORIGINAL exception — callers' existing error
            # handling and toast messages reference Emergent, not Anthropic.
            raise primary_exc


__all__ = [
    "send_with_fallback",
    "_is_claude_model",   # exported for tests
    "_is_transient",      # exported for tests
    "_direct_anthropic_call",  # exported for tests
]
