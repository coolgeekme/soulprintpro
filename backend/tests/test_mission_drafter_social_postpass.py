"""Regression: the mission drafter MUST deterministically force the
`draft_to_social_queue` tool for social-media post requests, regardless
of what the underlying LLM hallucinates this turn.

Bug history: user ran "Generate 7 IG posts" twice in a row. First time
it correctly drafted to the Social Queue. Second time, the drafter LLM
produced a `.pdf` deliverable goal and the mission lead followed
through, dumping the posts into a PDF instead of the Queue.

Fix: `draft_mission_goal` now post-processes the LLM output by running
a deterministic regex (`_has_social_intent`) over the RAW user input.
If social intent is detected AND the polished goal either (a) lacks
`draft_to_social_queue` or (b) mentions a doc-shaped deliverable (.pdf
/.md/.docx/.txt/write_artifact), we append a hard
`_SOCIAL_QUEUE_DIRECTIVE` to the end of the polished goal.

These tests exercise both the detector and the post-pass logic
directly — no LLM round-trip required.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from missions import (  # noqa: E402
    _has_social_intent,
    _SOCIAL_QUEUE_DIRECTIVE,
)


# ── _has_social_intent ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Generate 7 IG posts about Disciplined Hustle",
        "Create 3 instagram posts for the spring drop",
        "Draft 5 LinkedIn posts about our new product launch",
        "Write 10 tweets about AI agents",
        "Make 4 tiktok captions for the holiday campaign",
        "Queue up 6 facebook posts",
        "Schedule a week of IG reels captions",
        "Prepare a thread on Bluesky about our open-source release",
        "draft 2 youtube shorts captions",
        "Create 3 social media posts for the announcement",
        "Write 8 reels captions",
        # platform alias only (ig / li / fb / tt / yt) with verb
        "ig posts — generate 5",
        # plural with platform inline
        "post 3 instagram updates about pricing",
    ],
)
def test_detector_catches_social_intent(text):
    assert _has_social_intent(text), (
        f"_has_social_intent should return True for: {text!r}"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Write a 1-page PDF brief on the future of AI agents",
        "Build a CSV list of 25 nonprofits in Austin",
        "Audit my SEO and write a report",
        "Research the top 5 competitors and write a teardown",
        "Generate a logo concept",
        "Create an HTML snippet for a watermark",
        "",
        "   ",
        # Platform word alone is NOT enough — must have a content verb
        # OR a post-shaped noun.
        "Find me an instagram influencer",
    ],
)
def test_detector_ignores_non_social_intent(text):
    assert not _has_social_intent(text), (
        f"_has_social_intent should return False for: {text!r}"
    )


# ── _SOCIAL_QUEUE_DIRECTIVE shape ─────────────────────────────────────


def test_directive_names_the_tool_and_forbids_files():
    d = _SOCIAL_QUEUE_DIRECTIVE.lower()
    assert "draft_to_social_queue" in d
    assert "write_artifact" in d  # explicitly forbidden
    # Must forbid every doc-extension the mission lead could fall back to
    for ext in (".pdf", ".md", ".docx", ".txt"):
        assert ext in d, f"Directive should forbid {ext}"
    # Must end with a clear finish directive so the lead knows when done
    assert "finish" in d


# ── End-to-end post-pass simulation ───────────────────────────────────


def _simulate_postpass(raw_input: str, polished: str) -> str:
    """Mirror the exact post-pass logic inside draft_mission_goal so we
    can regression-test it without spinning up FastAPI / the LLM."""
    import re as _re
    if _has_social_intent(raw_input):
        needs_directive = "draft_to_social_queue" not in polished
        mentions_doc = bool(
            _re.search(
                r"\.(pdf|md|docx|doc|txt|rtf)\b|\bwrite_artifact\b",
                polished,
                _re.IGNORECASE,
            )
        )
        if needs_directive or mentions_doc:
            polished = polished.rstrip() + _SOCIAL_QUEUE_DIRECTIVE
    return polished


def test_postpass_appends_when_drafter_returns_pdf():
    raw = "Generate 7 IG posts about Disciplined Hustle"
    hallucinated = (
        "Produce `instagram_posts.pdf` — a 7-post brief covering the "
        "Disciplined Hustle theme. Use `write_artifact` with mime "
        "application/pdf. Definition of done: PDF saved."
    )
    out = _simulate_postpass(raw, hallucinated)
    assert _SOCIAL_QUEUE_DIRECTIVE in out
    assert "draft_to_social_queue" in out
    assert ".pdf" in out  # the original (hallucinated) text is preserved
    # …but the override is on the end so the lead reads it last and obeys


def test_postpass_appends_when_drafter_returns_markdown():
    raw = "Create 3 LinkedIn posts about our launch"
    hallucinated = (
        "Produce `linkedin_posts.md` with 3 ready-to-publish posts. "
        "Use write_artifact. Definition of done: file saved."
    )
    out = _simulate_postpass(raw, hallucinated)
    assert _SOCIAL_QUEUE_DIRECTIVE in out


def test_postpass_does_not_double_append_when_already_correct():
    raw = "Draft 5 tweets about our new feature"
    correct = (
        "Use `draft_to_social_queue` with platforms=['twitter'], count=5, "
        "and a prompt that captures the brand voice. Definition of done: "
        "5 drafts in the Social Queue → Drafts tab."
    )
    out = _simulate_postpass(raw, correct)
    # No directive was needed; the polished goal already names the tool.
    assert out == correct
    assert out.count("draft_to_social_queue") == 1


def test_postpass_is_noop_for_non_social_goals():
    raw = "Audit my SEO and write a 1-page PDF brief"
    polished = (
        "Produce `seo_audit.pdf` — a 1-page audit of the site. "
        "Definition of done: PDF saved."
    )
    out = _simulate_postpass(raw, polished)
    # Non-social input → post-pass must not touch the polished goal.
    assert out == polished
    assert _SOCIAL_QUEUE_DIRECTIVE not in out
