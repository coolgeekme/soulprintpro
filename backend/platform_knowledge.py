"""Foundry platform knowledge injector.

When a user asks "what is Foundry?", "what can it do?", "who built
this?", "tell me about the platform", etc., the chat layer needs to
answer from an authoritative source instead of relying on whatever the
LLM happened to internalise about a B2B SaaS product.

This module:
1. **Loads** the bundled product-knowledge `.md` files at import:
     - `backend/knowledge/foundry.md` — short canonical FAQ
     - `docs/FOUNDRY_FEATURES.md`     — feature catalogue
     - `docs/FOUNDRY_WHITEPAPER.md`   — long-form positioning + arch
     - `docs/SOC2_READINESS.md`       — security / compliance posture
     - `memory/ABOUT.md`              — company / team / story
   Each file is concatenated under a `── SOURCE: <filename> ──`
   header so the LLM can cite which doc it pulled an answer from.
2. **Detects** "what-is-Foundry" intent via a deterministic regex over
   the user's latest message (`has_foundry_question`).
3. **Returns** a prompt-ready block (`get_foundry_context_block`) that
   `build_system_prompt` appends to the agent system prompt for that
   turn only.

Why a regex and not "always inject"? The bundle is ~90 KB / ~23K
tokens — small enough to fit comfortably in GPT-4.1-mini's 128K
context but expensive to ship on every turn. Per-turn intent
detection keeps the cost on the small fraction of turns where the
user is genuinely asking about the product.

The file content is **never paraphrased** — it's authoritative. The
prompt header tells the agent to answer in their own voice but stay
faithful to the facts in this block. Anything not covered → "I don't
know" rather than fabrication.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional
import re

log = logging.getLogger("platform_knowledge")

# Repo root resolves to `/app` in the deploy artifact. We anchor to
# this file's location so the loader works in both dev and prod and
# doesn't depend on CWD.
_REPO_ROOT = Path(__file__).resolve().parent.parent  # /app

# Explicit whitelist of files that are safe to surface to end users.
# Anything in `/app/memory/` other than ABOUT.md is dev-internal
# (PRD.md, CHANGELOG.md, test_credentials.md, …) and MUST stay out.
_KNOWLEDGE_FILES: List[Path] = [
    _REPO_ROOT / "backend" / "knowledge" / "foundry.md",
    _REPO_ROOT / "docs" / "FOUNDRY_FEATURES.md",
    _REPO_ROOT / "docs" / "FOUNDRY_WHITEPAPER.md",
    _REPO_ROOT / "docs" / "SOC2_READINESS.md",
    _REPO_ROOT / "memory" / "ABOUT.md",
]


def _load_knowledge() -> str:
    """Concatenate every whitelisted .md under labelled section headers.

    Missing files are skipped silently — the deploy artifact MAY ship
    without one of them (e.g. `SOC2_READINESS.md` could be enterprise-
    only) and that shouldn't blow up the chat layer.
    """
    chunks: List[str] = []
    for path in _KNOWLEDGE_FILES:
        try:
            if not path.is_file():
                continue
            body = path.read_text(encoding="utf-8").strip()
            if not body:
                continue
            # Pretty file label: repo-relative path so the agent can
            # cite "per the FOUNDRY_FEATURES.md doc" in its reply.
            try:
                rel = path.relative_to(_REPO_ROOT).as_posix()
            except Exception:
                rel = path.name
            chunks.append(f"── SOURCE: {rel} ──\n{body}")
        except Exception as exc:
            log.warning("Foundry knowledge load failed for %s: %s", path, exc)
    if not chunks:
        return ""
    return "\n\n".join(chunks)


# Loaded once at import. The files are bundled into the deploy
# artifact; updates require a redeploy.
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
    r"what\s+does\s+(?:the\s+)?foundry\s+(?:do|offer|include)|"
    r"tell\s+me\s+(?:more\s+)?about\s+(?:the\s+)?foundry|"
    r"explain\s+(?:the\s+)?foundry|"
    r"about\s+(?:the\s+)?foundry(?:\s+platform)?|"
    r"foundry(?:\s+platform)?\s+overview|"
    r"how\s+does\s+(?:the\s+)?foundry\s+work|"
    r"foundry'?s?\s+(?:features|capabilities|pricing|plans|tiers|security|architecture)|"
    # Anonymous "this app / this platform" variants — common when users
    # know they're chatting via Foundry but not the name.
    r"what(?:'s| is| does)\s+(?:this|the)\s+(?:platform|app|product|tool|service)(?:\s+do)?|"
    r"tell\s+me\s+about\s+(?:this|the)\s+(?:platform|app|product|tool|service)|"
    r"what\s+can\s+(?:this(?:\s+(?:platform|app|product|tool))?|the\s+platform|foundry)\s+do(?:\s+for\s+me)?|"
    r"what\s+are\s+(?:your|the\s+platform's)\s+(?:features|capabilities)|"
    r"capabilities\s+of\s+(?:this|the)\s+(?:platform|app)|"
    # Security / compliance probes — answered by SOC2_READINESS.md
    r"(?:is\s+)?foundry\s+(?:soc\s*2|soc2|gdpr|hipaa|iso\s*27001)|"
    r"how\s+(?:secure|safe)\s+is\s+(?:foundry|this|the\s+platform)|"
    r"(?:foundry'?s?\s+)?(?:security|compliance|privacy)\s+(?:posture|model|policy)|"
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
        "The blocks below are concatenated from the AUTHORITATIVE "
        "product docs shipped with this deploy (FOUNDRY_FEATURES.md, "
        "FOUNDRY_WHITEPAPER.md, SOC2_READINESS.md, ABOUT.md, and the "
        "canonical FAQ). Each section is preceded by `── SOURCE: <path> ──` "
        "so you can cite the source if helpful. Answer in your own "
        "voice and on-brand for the org, but stay STRICTLY faithful to "
        "the facts here. If the user asks about something not in these "
        "blocks, say honestly that you don't know — DO NOT fabricate."
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
