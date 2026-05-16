"""TTS helper for Telegram replies.

A single function: ``tts_to_opus(text)`` — returns OGG/Opus bytes that
Telegram's ``sendVoice`` will render as a native voice note (waveform +
play button) instead of a generic audio attachment.

We chunk text > 3500 chars (OpenAI cap is 4096) and concatenate the
resulting opus streams. Tiny imperfection: streams concatenated raw
don't share an OGG header, so for now we cap at one chunk and truncate
beyond that. Multi-chunk concatenation is a follow-up if real users
generate replies longer than 3500 chars (unlikely for a CoS bot).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger("foundry.forge_tts")

# Default TTS settings tuned for the Telegram CoS use case: cheap+fast
# tts-1, energetic friendly voice, slight speed-up so it feels more
# conversational than robot-y. Override via env if a deploy wants HD.
_DEFAULT_MODEL = os.environ.get("TELEGRAM_TTS_MODEL", "tts-1")
_DEFAULT_VOICE = os.environ.get("TELEGRAM_TTS_VOICE", "nova")
_DEFAULT_SPEED = float(os.environ.get("TELEGRAM_TTS_SPEED", "1.05"))

# OpenAI's hard cap is 4096; we leave headroom for prompt wrappers.
_MAX_TTS_CHARS = 3500


def _strip_html(text: str) -> str:
    """Remove the small set of HTML tags our nudges use (<b>, <i>) so
    they don't end up being read aloud literally ('less than b greater
    than'). Anything else gets passed through verbatim."""
    if not text:
        return ""
    no_tags = re.sub(r"</?[a-z][a-z0-9]*[^>]*>", "", text, flags=re.I)
    # Unescape the small set of entities format_nudge uses.
    return (
        no_tags
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .strip()
    )


async def tts_to_opus(
    text: str,
    *,
    voice: Optional[str] = None,
    model: Optional[str] = None,
    speed: Optional[float] = None,
) -> Optional[bytes]:
    """Synthesise `text` to OGG/Opus bytes. Returns None on failure
    (the caller should fall back to a regular text reply)."""
    text = _strip_html(text or "")
    if not text:
        return None
    if len(text) > _MAX_TTS_CHARS:
        # Keep the spoken reply concise. Telegram's voice-note bubble
        # actively discourages long monologues — the UX expectation is
        # < 30s. 3500 chars ≈ 4 min at default speed, which is way past
        # the bubble's usefulness anyway.
        text = text[:_MAX_TTS_CHARS] + "… I trimmed the rest — see the chat for details."
    key = os.environ.get("EMERGENT_LLM_KEY", "")
    if not key:
        logger.warning("TTS skipped: EMERGENT_LLM_KEY not set")
        return None
    try:
        from emergentintegrations.llm.openai import OpenAITextToSpeech
    except Exception as exc:
        logger.warning("OpenAITextToSpeech import failed: %s", exc)
        return None
    tts = OpenAITextToSpeech(api_key=key)
    try:
        audio_bytes = await tts.generate_speech(
            text=text,
            model=model or _DEFAULT_MODEL,
            voice=voice or _DEFAULT_VOICE,
            speed=speed if speed is not None else _DEFAULT_SPEED,
            response_format="opus",
        )
        return audio_bytes
    except Exception as exc:
        logger.exception("TTS generation failed: %s", exc)
        return None
