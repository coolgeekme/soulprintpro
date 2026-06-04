"""Regression tests for the transcript-paste false-positive that misrouted
a 'summarize this transcript' message into video generation.

Production bug (Feb 2026): user asked their Chief of Staff to summarize a
Plaud-style transcript. The transcript was full of bracketed pause markers
like ``[...0.6s]``, ``[...2.5s]``, ``[...0.5s]``. The OLD regex
``\\d+\\s*[-]?\\s*(second|sec|s)\\s*(video|clip|spot|ad)?`` made the
trailing video noun OPTIONAL, so bare ``"0.6s"`` / ``"2.5s"`` matched.
Combined with verbs like "create" / "make" elsewhere in the transcript,
the fast-path ``_quick_video_intent`` returned True without consulting
the LLM classifier — and the entire transcript got shipped to Kie.ai as
a video prompt that never came back.

Tests verify:
  • Bare timestamp markers no longer match ``_VIDEO_KEYWORDS_RE``.
  • Real video requests ("create a 30 second commercial") still match.
  • ``_looks_like_transcript_paste`` triggers on >=3 markers.
  • ``_quick_video_intent`` returns False for transcripts.
  • ``_quick_image_intent`` also short-circuits on transcripts.
"""
from __future__ import annotations

import server


# ── Regex no longer false-positives on bare timestamps ──────────────────


def test_video_regex_does_not_match_bare_timestamp() -> None:
    """Bare `0.6s` / `2.5s` / `30s` MUST NOT match — those are pause
    markers from Plaud / Otter / Granola transcripts."""
    for needle in [
        "0.6s",
        "2.5s",
        "30s",
        "[...0.6s]",
        "[2.5s]",
        "she paused 5s before answering",
    ]:
        assert server._VIDEO_KEYWORDS_RE.search(needle) is None, (
            f"timestamp '{needle}' should NOT match the video keyword regex"
        )


def test_video_regex_still_matches_real_duration_requests() -> None:
    """Real video asks like '30 second video' / '5 second commercial'
    MUST still match — we only tightened the noun, not the duration."""
    for needle in [
        "30 second video",
        "5-second clip",
        "make a 60 second ad",
        "10 sec reel",
        "8 second teaser",
        "15-second spot",
        "do a 7s short for me",
    ]:
        assert server._VIDEO_KEYWORDS_RE.search(needle) is not None, (
            f"'{needle}' must still match the video keyword regex"
        )


def test_video_regex_still_matches_keyword_only() -> None:
    """Non-duration video keywords are unaffected by the fix."""
    for needle in [
        "make a video",
        "create an animation",
        "youtube video",
        "tiktok reel",
        "b-roll for the launch",
        "cinemagraph",
    ]:
        assert server._VIDEO_KEYWORDS_RE.search(needle) is not None


# ── Transcript-paste detector ───────────────────────────────────────────


def test_transcript_paste_detected_with_three_markers() -> None:
    text = "Hi [...0.6s] there [...2.5s] friend [...1.0s] bye."
    assert server._looks_like_transcript_paste(text) is True


def test_transcript_paste_not_triggered_by_a_single_marker() -> None:
    """A single mention like 'I added a [silence 2s] gap' is NOT a
    transcript paste — we require ≥3 markers."""
    text = "I added a [silence 2s] gap between the scenes."
    assert server._looks_like_transcript_paste(text) is False


def test_transcript_paste_handles_alternate_unit_formats() -> None:
    """Other transcribers write `[pause 1.5s]`, `[0.5s]`, `[...3s]`. All
    of these MUST count toward the threshold."""
    text = "[pause 1.5s] hi [0.5s] there [...3s] bye [...3s]"
    assert server._looks_like_transcript_paste(text) is True


# ── End-to-end intent classifiers ───────────────────────────────────────


SUMMARIZE_TRANSCRIPT = (
    "summarize this transcript:\n\n"
    "[...0.6s]What's up? [...2.5s] Too much, not too much, [...1.5s] perfect, cool. "
    "I just, I just added a new feature on our Soulprint engine to [...1.1s] "
    "for users to create, kind of like, we call them imprints. So it's basically, "
    "like, [...0.6s] an instruction [...1.3s] to your AI thing.So for example, "
    "I created my custom one. It's not quite an agent, but a custom one to help "
    "me find scholarships for my oldest."
)


def test_quick_video_intent_rejects_summarize_transcript_paste() -> None:
    """The exact regression from production. With the OLD regex this
    returned True (bug). With the fix it MUST return False so the
    summarize request flows through normal chat instead of Kie.ai."""
    out = server._quick_video_intent(SUMMARIZE_TRANSCRIPT)
    assert out is False, (
        "summarize-transcript request was misrouted to video gen — "
        "the transcript-paste veto failed to fire"
    )


def test_quick_image_intent_rejects_summarize_transcript_paste() -> None:
    out = server._quick_image_intent(SUMMARIZE_TRANSCRIPT)
    assert out is False


def test_quick_video_intent_still_yes_for_real_video_request() -> None:
    """The fix must NOT regress legitimate video requests."""
    assert server._quick_video_intent("Make me a 30 second commercial for our launch") is True
    assert server._quick_video_intent("Create a video about our pricing") is True


def test_quick_video_intent_neutral_for_ambiguous_text() -> None:
    """Plain text with no video keywords and no transcript markers
    returns None (defer to LLM classifier)."""
    assert server._quick_video_intent("Tell me about Q3 strategy.") is None
