"""Regression tests for the Mission ask_user guard fixes (Feb 2026).

User-reported production bug: missions kept re-asking the same
clarifying question (e.g. "provide sources for AstraZeneca's Virginia
plant?") even after the user explicitly said "I have nothing more to
add / please finish / I've already replied."

Three guards in play:
    1. Regex `_looks_like_user_declined` flags such answers so the
       backend sets `user_declined_more_info=True` on the mission.
    2. `MAX_ASK_USER_PER_MISSION` caps total ask_user calls per mission.
    3. POST /api/missions/{id}/force-finish lets the user nuke any
       remaining questions and force the loop to deliver.

These tests exercise the pure helpers directly — the loop-integration
behaviour is exercised by the integration tests already in place for
mission lifecycle.
"""

import pytest

from missions import (
    _looks_like_user_declined,
    MAX_ASK_USER_PER_MISSION,
)


@pytest.mark.parametrize("text", [
    "I have nothing more to add",
    "Please finish the research",
    "proceed",
    "Just do it",
    "I already replied multiple times",
    "use what you have",
    "skip",
    "no more info",
    "stop asking",
    "I've answered this twice already",
    "no further information available",
    "wrap it up",
    "carry on without me",
    "don't ask again",
    "go ahead",
    "complete the mission",
    "nothing to add",
])
def test_declined_phrases_match(text):
    assert _looks_like_user_declined(text), f"Should have matched: {text!r}"


@pytest.mark.parametrize("text", [
    "here is the URL: https://example.com",
    "the company is based in Virginia",
    "try this source instead",
    "Acme Corp employs about 200 people",
    "yes",  # ambiguous one-word — we'd rather under-match than over-match
    "the SEC filing is at sec.gov/...",
    "",
    None,
])
def test_real_information_does_not_match(text):
    # We err on the side of treating "yes" / blank as not-declined so
    # users who actually do have new info don't get the guard tripped.
    assert not _looks_like_user_declined(text or "")


def test_max_ask_user_is_a_real_cap():
    # Anchored at 2 in the fix; bump deliberately if you ever raise it.
    assert MAX_ASK_USER_PER_MISSION == 2
