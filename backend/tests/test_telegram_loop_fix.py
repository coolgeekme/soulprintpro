"""Tests for the Telegram-side loop-prevention helpers.

These cover the Feb 2026 bug where the Chief-of-Staff bot would emit
multiple `[[IMAGE]]` blocks in one reply, causing the Telegram user to
see what looked like the bot "spamming drafts unprompted" — five photos
arriving 5-15 seconds apart as Nano Banana finished each call.

The fix caps Telegram-side image blocks to a single block per turn and
folds the rest into a one-line nudge.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import the cap helper directly from server.py without booting the
# whole FastAPI app — the helper is pure / synchronous.
from server import _cap_image_blocks_for_telegram  # type: ignore  # noqa: E402


def test_single_block_passes_through_unchanged():
    reply = (
        "Here's the IG post:\n\n"
        '[[IMAGE prompt="brand hero, calm blue" aspect="1:1"]]\n\n'
        "Caption: Same-day appointments — book now."
    )
    out = _cap_image_blocks_for_telegram(reply, max_blocks=1)
    assert out == reply


def test_no_block_passes_through_unchanged():
    reply = "Plain text reply — no image."
    out = _cap_image_blocks_for_telegram(reply, max_blocks=1)
    assert out == reply


def test_caps_multiple_blocks_to_one_and_adds_nudge():
    reply = (
        "Three variants for you:\n"
        '[[IMAGE prompt="Variant 1" aspect="1:1"]]\n'
        '[[IMAGE prompt="Variant 2" aspect="1:1"]]\n'
        '[[IMAGE prompt="Variant 3" aspect="1:1"]]\n'
        "Pick your favorite."
    )
    out = _cap_image_blocks_for_telegram(reply, max_blocks=1)
    # Only the FIRST block should remain.
    assert out.count("[[IMAGE") == 1
    assert "Variant 1" in out
    assert "Variant 2" not in out
    assert "Variant 3" not in out
    # Held nudge should mention 2 dropped blocks.
    assert "Held 2 additional draft visual" in out
    # Surrounding prose preserved.
    assert "Three variants" in out
    assert "Pick your favorite" in out


def test_held_nudge_singular_vs_plural():
    reply = (
        '[[IMAGE prompt="A"]]\n'
        '[[IMAGE prompt="B"]]'
    )
    out = _cap_image_blocks_for_telegram(reply, max_blocks=1)
    assert "Held 1 additional draft visual " in out
    # No plural-s on the count of "visual" when only one was dropped.
    assert "Held 1 additional draft visuals" not in out


def test_max_blocks_two_keeps_first_two():
    reply = (
        '[[IMAGE prompt="A"]]\n'
        '[[IMAGE prompt="B"]]\n'
        '[[IMAGE prompt="C"]]\n'
    )
    out = _cap_image_blocks_for_telegram(reply, max_blocks=2)
    assert out.count("[[IMAGE") == 2
    assert "A" in out and "B" in out
    assert "C" not in out
    assert "Held 1 additional" in out


def test_empty_reply_returns_empty():
    assert _cap_image_blocks_for_telegram("", max_blocks=1) == ""
    assert _cap_image_blocks_for_telegram(None, max_blocks=1) == ""  # type: ignore[arg-type]
