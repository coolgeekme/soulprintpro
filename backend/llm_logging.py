"""LLM telemetry — record every model call for the admin dashboard.

We hook two surfaces:
    1. ``emergentintegrations.llm.chat.LlmChat.send_message`` — every chat,
       routing, mission, social, and soul-generation call.
    2. ``emergentintegrations.llm.chat.LlmChat.send_message_multimodal_response``
       — image-generation chat path used for Nano Banana inline edits.

Both are monkey-patched ONCE on import. We layer on top of the existing
``llm_fallback.install_fallback()`` so the call chain becomes:

    user code -> [logging wrapper] -> [fallback wrapper] -> [original SDK]

What's recorded (one row per call) in collection ``llm_calls``::

    {
        provider: "openai" | "anthropic" | "gemini",
        model: "gpt-4o-mini",
        kind: "chat" | "image" | "multimodal",
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cost_usd: float,            # estimate from MODEL_RATES
        latency_ms: int,
        ok: bool,
        error: str,                  # if not ok
        org_id: str | None,          # injected via ``with_call_context``
        user_id: str | None,
        mission_id: str | None,
        conversation_id: str | None,
        agent_id: str | None,
        kind_label: str,             # "mission_lead" | "agent_reply" | etc
        created_at: datetime,
    }

Costs are **estimates** — we use public 2026 list pricing per 1K tokens.
Real billing happens upstream (Emergent universal key); we surface a
useful proxy so the operator can see relative spend by org/model/day.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from emergentintegrations.llm.chat import LlmChat

logger = logging.getLogger(__name__)


# Public 2026 list pricing (USD per 1K tokens). When a model isn't found,
# we fall back to GPT-4o-mini-equivalent estimates so the operator still
# sees a number rather than $0. Update these when official pricing changes.
MODEL_RATES = {
    # OpenAI text
    "gpt-4o":              {"input": 0.0025, "output": 0.010},
    "gpt-4o-mini":         {"input": 0.00015, "output": 0.0006},
    "gpt-4.1":             {"input": 0.002,  "output": 0.008},
    "gpt-4.1-mini":        {"input": 0.0004, "output": 0.0016},
    "gpt-5":               {"input": 0.005,  "output": 0.015},
    "gpt-5-mini":          {"input": 0.0005, "output": 0.002},
    "gpt-5.2":             {"input": 0.005,  "output": 0.015},
    # OpenAI image
    "gpt-image-1":         {"per_image": 0.04},
    # Anthropic
    "claude-sonnet-4.5":   {"input": 0.003,  "output": 0.015},
    "claude-haiku-4.5":    {"input": 0.0008, "output": 0.004},
    "claude-opus-4.5":     {"input": 0.015,  "output": 0.075},
    # Gemini
    "gemini-2.5-pro":      {"input": 0.00125, "output": 0.005},
    "gemini-2.5-flash":    {"input": 0.00015, "output": 0.0006},
    "gemini-2.5-flash-image-preview": {"per_image": 0.039},  # Nano Banana
    "nano-banana":         {"per_image": 0.039},
}

DEFAULT_TEXT_RATE = {"input": 0.001, "output": 0.003}
DEFAULT_IMAGE_RATE = {"per_image": 0.04}


# Contextvar lets call sites attach metadata that we read inside the
# wrapper (org_id, user_id, mission_id, conversation_id, agent_id).
# Each request handler should call ``with_call_context(...)`` near the top.
_CALL_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("llm_call_ctx", default={})


def with_call_context(**kwargs) -> contextvars.Token:
    """Attach metadata for the duration of this async task. Returns a token
    to ``reset`` when leaving the scope (use ``set_call_context`` for a
    fire-and-forget set in long handlers)."""
    return _CALL_CTX.set({**(_CALL_CTX.get() or {}), **{k: v for k, v in kwargs.items() if v is not None}})


def reset_call_context(token: contextvars.Token) -> None:
    try:
        _CALL_CTX.reset(token)
    except Exception:
        pass


def _resolve_rate(model: str) -> dict:
    if not model:
        return DEFAULT_TEXT_RATE
    m = model.lower()
    if m in MODEL_RATES:
        return MODEL_RATES[m]
    # Best-effort prefix match (handles 'gpt-4o-2024-08-06' style versions).
    for k, v in MODEL_RATES.items():
        if m.startswith(k):
            return v
    if "image" in m or "banana" in m or "dalle" in m:
        return DEFAULT_IMAGE_RATE
    return DEFAULT_TEXT_RATE


def estimate_cost(model: str, prompt_tokens: int = 0, completion_tokens: int = 0,
                  images: int = 0) -> float:
    """Rough USD cost for a single call. Token-based for chat, per-image for
    image generation. Always returns a non-negative float."""
    rate = _resolve_rate(model)
    if "per_image" in rate and images > 0:
        return float(rate["per_image"]) * images
    pin = float(rate.get("input", DEFAULT_TEXT_RATE["input"]))
    pout = float(rate.get("output", DEFAULT_TEXT_RATE["output"]))
    return (prompt_tokens / 1000.0) * pin + (completion_tokens / 1000.0) * pout


def _approx_tokens(text: str) -> int:
    """When the SDK doesn't return token counts (LlmChat.send_message
    returns just a string), approximate as 4 chars per token. Off by ~25%
    in practice, but plenty good for a relative spend dashboard."""
    if not text:
        return 0
    return max(1, len(text) // 4)


async def _record(db, doc: dict) -> None:
    """Insert one row. Best-effort — never blocks or raises into caller."""
    try:
        await db.llm_calls.insert_one(doc)
    except Exception as exc:
        logger.warning("llm_calls insert failed: %s", exc)


def install_logging(db) -> None:
    """Wrap LlmChat.send_message and send_message_multimodal_response.

    Idempotent — safe to call once at app boot. Layers on top of the
    fallback wrapper. Capture happens AFTER the original returns so we
    log even when fallback kicks in (we just record the model that
    actually ran, not the one originally requested).
    """
    if getattr(LlmChat, "_logging_installed", False):
        return

    original_send = LlmChat.send_message
    original_mm = getattr(LlmChat, "send_message_multimodal_response", None)

    async def send_with_logging(self, user_message):
        t0 = time.perf_counter()
        ok = True
        err = ""
        text_out = ""
        try:
            text_out = await original_send(self, user_message)
            return text_out
        except Exception as exc:
            ok = False
            err = str(exc)[:400]
            raise
        finally:
            try:
                latency_ms = int((time.perf_counter() - t0) * 1000)
                model = getattr(self, "model", "") or ""
                provider = getattr(self, "provider", "") or ""
                user_text = getattr(user_message, "text", "") or ""
                # Sum the entire conversation's user/system/assistant tokens
                # so we count the actual prompt that hit the wire.
                history = getattr(self, "messages", []) or []
                history_text = ""
                for m in history[-30:]:  # cap to last 30 turns to keep it cheap
                    c = (m.get("content") if isinstance(m, dict) else None) or ""
                    if isinstance(c, str):
                        history_text += c
                prompt_tokens = _approx_tokens(history_text + user_text + (getattr(self, "system_message", "") or ""))
                completion_tokens = _approx_tokens(text_out or "")
                cost = estimate_cost(model, prompt_tokens, completion_tokens)
                ctx = _CALL_CTX.get() or {}
                doc = {
                    "provider": provider,
                    "model": model,
                    "kind": "chat",
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "cost_usd": round(cost, 6),
                    "latency_ms": latency_ms,
                    "ok": ok,
                    "error": err,
                    "kind_label": ctx.get("kind_label", ""),
                    "org_id": ctx.get("org_id"),
                    "user_id": ctx.get("user_id"),
                    "mission_id": ctx.get("mission_id"),
                    "conversation_id": ctx.get("conversation_id"),
                    "agent_id": ctx.get("agent_id"),
                    "created_at": datetime.now(timezone.utc),
                }
                # Fire and forget — never block the caller.
                asyncio.create_task(_record(db, doc))
            except Exception as inner:
                logger.warning("llm logging hook failed: %s", inner)

    LlmChat.send_message = send_with_logging

    if original_mm:
        async def mm_with_logging(self, user_message):
            t0 = time.perf_counter()
            ok = True
            err = ""
            result = None
            try:
                result = await original_mm(self, user_message)
                return result
            except Exception as exc:
                ok = False
                err = str(exc)[:400]
                raise
            finally:
                try:
                    latency_ms = int((time.perf_counter() - t0) * 1000)
                    model = getattr(self, "model", "") or ""
                    provider = getattr(self, "provider", "") or ""
                    # Result is a tuple (text, [bytes]) for multimodal.
                    images = 0
                    text_out = ""
                    if isinstance(result, tuple) and len(result) >= 2:
                        text_out = (result[0] or "")
                        imgs = result[1] or []
                        try:
                            images = len(imgs)
                        except Exception:
                            images = 1 if imgs else 0
                    user_text = getattr(user_message, "text", "") or ""
                    prompt_tokens = _approx_tokens(user_text + (getattr(self, "system_message", "") or ""))
                    completion_tokens = _approx_tokens(text_out)
                    cost = estimate_cost(model, prompt_tokens, completion_tokens, images=images)
                    ctx = _CALL_CTX.get() or {}
                    doc = {
                        "provider": provider,
                        "model": model,
                        "kind": "image" if images else "multimodal",
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "images": images,
                        "total_tokens": prompt_tokens + completion_tokens,
                        "cost_usd": round(cost, 6),
                        "latency_ms": latency_ms,
                        "ok": ok,
                        "error": err,
                        "kind_label": ctx.get("kind_label", "image_gen"),
                        "org_id": ctx.get("org_id"),
                        "user_id": ctx.get("user_id"),
                        "mission_id": ctx.get("mission_id"),
                        "conversation_id": ctx.get("conversation_id"),
                        "agent_id": ctx.get("agent_id"),
                        "created_at": datetime.now(timezone.utc),
                    }
                    asyncio.create_task(_record(db, doc))
                except Exception as inner:
                    logger.warning("llm mm logging hook failed: %s", inner)

        LlmChat.send_message_multimodal_response = mm_with_logging

    LlmChat._logging_installed = True
    logger.info("LLM telemetry logging installed.")


__all__ = [
    "MODEL_RATES",
    "estimate_cost",
    "with_call_context",
    "reset_call_context",
    "install_logging",
]
