"""LLM fallback shim.

When the Emergent universal-key call fails with "Budget has been exceeded",
transparently retry the same call against the user's own OpenAI key
(`OPENAI_API_KEY` in `/app/backend/.env`).

We monkey-patch `emergentintegrations.llm.chat.LlmChat.send_message` exactly
once on import. This way every existing call site in the codebase keeps
working — we add a safety net rather than rewriting them.
"""
from __future__ import annotations

import logging
import os

from emergentintegrations.llm.chat import LlmChat, ChatError

logger = logging.getLogger(__name__)

OPENAI_FALLBACK_KEY = os.environ.get("OPENAI_API_KEY", "")

def _is_budget_error(exc: BaseException) -> bool:
    msg = (str(exc) or "").lower()
    return ("budget" in msg and "exceeded" in msg) or "insufficient_quota" in msg

async def _fallback_completion(chat_self, user_message) -> str:
    """Re-issue the same chat against OpenAI directly using the user's key."""
    from openai import AsyncOpenAI
    # Pull the conversation including the system message — LlmChat keeps the
    # running history on `self.messages`. The user's latest message hasn't
    # been added yet (the original send_message raised before that), so we
    # append it ourselves.
    history = list(getattr(chat_self, "messages", []) or [])
    user_text = getattr(user_message, "text", None) or ""
    history.append({"role": "user", "content": user_text})
    # If the original call was Anthropic / Gemini, we still fall back to
    # OpenAI gpt-4.1-mini — the user said any model is OK as long as the
    # call succeeds. Anthropic-tuned prompts work fine with gpt-4o family.
    model = getattr(chat_self, "model", "") or "gpt-4.1-mini"
    if getattr(chat_self, "provider", "openai") != "openai":
        model = "gpt-4.1-mini"
    client = AsyncOpenAI(api_key=OPENAI_FALLBACK_KEY)
    resp = await client.chat.completions.create(model=model, messages=history)
    text = (resp.choices[0].message.content or "").strip()
    history.append({"role": "assistant", "content": text})
    chat_self.messages = history  # keep history in sync for follow-up turns
    return text

def install_fallback() -> None:
    """Wrap LlmChat.send_message with a budget-aware retry. Idempotent."""
    if getattr(LlmChat, "_fallback_installed", False):
        return
    original = LlmChat.send_message

    async def send_message_with_fallback(self, user_message):
        try:
            return await original(self, user_message)
        except Exception as exc:
            if not OPENAI_FALLBACK_KEY:
                raise
            if not _is_budget_error(exc):
                raise
            try:
                logger.warning("Emergent budget exceeded — falling back to user OpenAI key")
                return await _fallback_completion(self, user_message)
            except Exception as fb_exc:
                # Surface the fallback error wrapped consistently with ChatError
                raise ChatError(f"Fallback OpenAI call failed: {fb_exc}") from fb_exc

    LlmChat.send_message = send_message_with_fallback
    LlmChat._fallback_installed = True
    logger.info(
        "LLM fallback installed (OpenAI direct key %s)",
        "configured" if OPENAI_FALLBACK_KEY else "NOT configured",
    )
