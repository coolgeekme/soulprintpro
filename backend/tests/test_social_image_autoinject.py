"""Tests for the social-post auto-image injector (safety net for
LLM forgetting to emit [[IMAGE]] blocks on social-media requests)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# We import the helpers directly to avoid pulling all of server.py
# (which would require Mongo, env vars, etc. just for unit tests of
# pure regex / string functions).
import re as _re

# Re-import the module-level statics under test. server.py is a big
# import so we do it lazily — fast-fail with a clear skip otherwise.
import pytest
try:
    from server import (
        _inject_social_image_block_if_missing,
        _SOCIAL_POST_TRIGGERS,
        _SOCIAL_POST_OPTOUT,
        _aspect_from_user_text,
    )
except Exception as exc:  # pragma: no cover
    pytest.skip(f"server.py not importable in this env: {exc}", allow_module_level=True)


def test_trigger_matches_common_phrasings():
    cases = [
        "Draft an IG post for AZ Pain Doctors",
        "I need a LinkedIn post for our launch",
        "Can you write a Facebook post about the new menu",
        "Draft a TikTok cover",
        "make me a youtube thumbnail",
        "We need an ad creative for the spring campaign",
        "draft a caption and image for the new feature",
    ]
    for c in cases:
        assert _SOCIAL_POST_TRIGGERS.search(c), f"failed to match: {c!r}"


def test_trigger_does_not_match_unrelated():
    cases = [
        "what's on my calendar today",
        "summarize the brief",
        "send Reggie an email",
        "what's the weather",
    ]
    for c in cases:
        assert not _SOCIAL_POST_TRIGGERS.search(c), f"unexpectedly matched: {c!r}"


def test_optout_phrasings():
    cases = [
        "Draft an IG post. Text only, I have my own photo.",
        "Make me a LinkedIn post, caption only please.",
        "Draft only the copy",
        "No image, just the words",
        "skip the visual",
        "I'll provide the image",
    ]
    for c in cases:
        assert _SOCIAL_POST_OPTOUT.search(c), f"opt-out missed: {c!r}"


def test_inject_adds_block_when_missing():
    team = {"name": "Acme", "soulprint": {"tone": "warm", "audience": "small biz"}}
    user = "Draft an IG post for Acme"
    reply = "Here's the caption: ..."
    out = _inject_social_image_block_if_missing(user, reply, team=team)
    assert "[[IMAGE" in out
    assert 'aspect="1:1"' in out


def test_inject_skips_when_block_already_present():
    team = {"name": "Acme"}
    user = "Draft an IG post for Acme"
    reply = (
        "Here's the post. [[IMAGE prompt=\"existing block\" aspect=\"1:1\"]]"
    )
    out = _inject_social_image_block_if_missing(user, reply, team=team)
    # Should not duplicate — exactly one [[IMAGE]] tag.
    assert out.upper().count("[[IMAGE") == 1


def test_inject_skips_when_user_opts_out():
    team = {"name": "Acme"}
    user = "Draft an IG post for Acme. Text only, I have my own image."
    reply = "Here's the caption only."
    out = _inject_social_image_block_if_missing(user, reply, team=team)
    assert "[[IMAGE" not in out.upper()


def test_inject_skips_when_user_message_not_social():
    team = {"name": "Acme"}
    user = "What is the weather in Tucson"
    reply = "Sunny and 78°F."
    out = _inject_social_image_block_if_missing(user, reply, team=team)
    assert "[[IMAGE" not in out.upper()


def test_aspect_detection():
    assert _aspect_from_user_text("Draft an IG story") == "9:16"
    assert _aspect_from_user_text("Make a TikTok cover") == "9:16"
    assert _aspect_from_user_text("YouTube thumbnail please") == "16:9"
    assert _aspect_from_user_text("LinkedIn launch banner") == "1.91:1"
    assert _aspect_from_user_text("IG post for Acme") == "1:1"  # default
    assert _aspect_from_user_text("portrait IG post 4:5") == "4:5"
