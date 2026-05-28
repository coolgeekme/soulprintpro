"""Foundry platform knowledge injector.

When a user asks "what is Foundry?", "what can it do?", "who built
this?", "tell me about the platform", etc., the chat layer needs to
answer from an authoritative source instead of relying on whatever the
LLM happened to internalise about a B2B SaaS product.

This module:
1. **Loads** `/app/backend/knowledge/foundry.md` once at import.
2. **Detects** "what-is-Foundry" intent via a deterministic regex over
   the user's latest message (`_has_foundry_question`).
3. **Returns** a prompt-ready block (`get_foundry_context_block`) that
   `build_system_prompt` appends to the agent system prompt for that
   turn only.

Why a regex and not "always inject"? The file is ~5 KB — small but
not free. We only need it on the small fraction of turns where the
user is actually asking about the product, so per-turn intent
detection saves tokens on every other turn.

The file content is **never paraphrased** — it's authoritative. The
prompt header tells the agent to answer in their own voice but stay
faithful to the facts in this block.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("platform_knowledge")

_KNOWLEDGE_FILE = Path(__file__).parent / "knowledge" / "foundry.md"


def _load_knowledge() -> str:
    try:
        if _KNOWLEDGE_FILE.is_file():
            return _KNOWLEDGE_FILE.read_text(encoding="utf-8").strip()
    except Exception as exc:
        log.warning("Foundry knowledge file load failed: %s", exc)
    return ""


# Loaded once at import. The file is bundled into the deploy artifact;
# updates require a redeploy (matches our "code-controlled v1" plan).
_FOUNDRY_KNOWLEDGE = _load_knowledge()


# Detector — fires when the user is *clearly* asking about the
# platform itself. Phrased broadly enough to catch the common ways
# someone asks ("what is foundry / foundry ai / foundryagents", "what
# can this do", "tell me about the platform", "who built this", "how
# does this work", "what's behind this app") but tight enough to skip
# unrelated mentions of "foundry" inside business names, etc.
_FOUNDRY_INTENT_RE = re.compile(
    r"\b("
    # Direct "what is Foundry" variants
    r"what(?:'s| is| are)\s+(?:the\s+)?foundry(?:\s*(?:platform|ai|agents))?|"
    r"tell\s+me\s+(?:more\s+)?about\s+(?:the\s+)?foundry|"
    r"explain\s+(?:the\s+)?foundry|"
    r"about\s+(?:the\s+)?foundry(?:\s+platform)?|"
    r"foundry(?:\s+platform)?\s+overview|"
    r"how\s+does\s+(?:the\s+)?foundry\s+work|"
    # Anonymous "this app / this platform" variants — common when users
    # know they're chatting via Foundry but not the name.
    r"what(?:'s| is| does)\s+(?:this|the)\s+(?:platform|app|product|tool|service)(?:\s+do)?|"
    r"tell\s+me\s+about\s+(?:this|the)\s+(?:platform|app|product|tool|service)|"
    r"what\s+can\s+(?:this(?:\s+(?:platform|app|product|tool))?|the\s+platform|foundry)\s+do(?:\s+for\s+me)?|"
    r"what\s+are\s+(?:your|the\s+platform's)\s+(?:features|capabilities)|"
    r"capabilities\s+of\s+(?:this|the)\s+(?:platform|app)|"
    # Provenance / "who built this"
    r"who\s+(?:built|made|created|developed)\s+(?:this|foundry|the\s+platform|the\s+app)|"
    r"who\s+(?:is|are)\s+(?:behind|the\s+team\s+behind)\s+(?:this|foundry)|"
    # Onboarding / getting started
    r"how\s+(?:do|can)\s+i\s+get\s+started(?:\s+with\s+foundry)?|"
    r"getting\s+started\s+(?:with\s+)?(?:foundry|this|the\s+platform)|"
    r"(?:list|show)\s+(?:me\s+)?(?:your|the)\s+features"
    r")\b",
    re.IGNORECASE,
)


def has_foundry_question(text: str) -> bool:
    """True iff the user's text is plausibly asking about Foundry-the-product."""
    if not text or not text.strip():
        return False
    return bool(_FOUNDRY_INTENT_RE.search(text))


def get_foundry_context_block(text: str) -> Optional[str]:
    """Return the prompt-injectable knowledge block, or None if the
    user isn't asking about Foundry / the file is missing.

    The returned string is ready to append to a system prompt — it
    includes its own header that frames the content as authoritative
    ground truth.
    """
    if not _FOUNDRY_KNOWLEDGE:
        return None
    if not has_foundry_question(text):
        return None
    header = (
        "── PRODUCT KNOWLEDGE: Foundry ──\n"
        "The user is asking about Foundry — the platform you live on. "
        "The block below is the AUTHORITATIVE description of Foundry's "
        "features, integrations, and current state. Answer in your own "
        "voice and on-brand for the org, but stay STRICTLY faithful to "
        "the facts here. If the user asks about something not in this "
        "block, say honestly that you don't know — DO NOT fabricate."
    )
    return f"{header}\n\n{_FOUNDRY_KNOWLEDGE}"


def reload_knowledge() -> bool:
    """Re-read the knowledge file. Useful for tests; in production the
    file is loaded once at import and any edit requires a redeploy.
    Returns True if the file was loaded successfully.
    """
    global _FOUNDRY_KNOWLEDGE
    _FOUNDRY_KNOWLEDGE = _load_knowledge()
    return bool(_FOUNDRY_KNOWLEDGE)


__all__ = [
    "has_foundry_question",
    "get_foundry_context_block",
    "reload_knowledge",
]
