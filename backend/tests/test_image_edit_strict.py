"""Regression: _quick_image_edit_intent (strict mode) must NOT trigger on
verbose strategy / brief / code messages that incidentally mention "image".

The bug the user reported was: a 2000-word client email about a website
strategy (full of "Collection image", "salami image", "product image"
phrases) was getting routed to image-edit mode, producing the unhelpful
"I can't find the source image to edit" reply.

The fix added three strict-mode guards: doc-intent veto, length cap (600
chars), and anaphoric-reference requirement. Test both that the user's
reproduction case is now vetoed AND that legitimate terse edit commands
still flow through.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import _quick_image_edit_intent  # noqa: E402


# The exact shape of the user's reproduction case — long client email
# about a website strategy that incidentally mentions image words.
USER_REPRO_MESSAGE = (
    "i received an email from the client:\n\n"
    "\"After reviewing the website more carefully, as mentioned, I would "
    "like to guide a few refinements so the platform reflects more "
    "precisely what is currently part of the business today, while still "
    "leaving space for the future direction we discussed together.\n\n"
    "One important distinction for me is separating:\n\n"
    "what is currently available\n"
    "what is part of the near-future development\n"
    "and what belongs more to the long-term brand vision\n"
    "Here are the main adjustments I would like to make:\n\n"
    "Contact email\n"
    "Please update the main contact email to:\n"
    "hello@emporiozeva.com\n"
    "NS monogram\n"
    "I like the NS monogram placed between the two lines. I would simply "
    "like it slightly smaller.\n"
    "Collection image\n"
    "In the collection section, I would prefer to use the salami image "
    "presented on the board rather than the current product image.\n"
    "Product direction and collection structure\n"
    "At this stage, I would like the website to stay very focused on the "
    "core hero product and the current corporate gifting direction.\""
)


# Verbose strategy / brief messages that incidentally mention image words
# but are NOT image-edit requests.
NEGATIVE_PROMPTS = [
    USER_REPRO_MESSAGE,
    # Code / snippet asks — caught by doc-intent veto.
    "Update the watermark HTML tag so it sits at the bottom-right corner.",
    "Change the CSS so the hero image scales correctly on mobile.",
    "Add a meta tag for the og:image so social previews pick it up.",
    # Strategy briefs — caught by length cap.
    "I'd like to update the brand strategy across the website. " * 25,
    # Edit-verb without an anaphoric image reference.
    (
        "Please update the main contact email and adjust the homepage "
        "copy. The salami image in the collection section should stay."
    ),
]


# Legitimately-terse image-edit commands that SHOULD still trigger the
# inline-edit path (so we don't over-correct).
POSITIVE_PROMPTS = [
    "make this image darker",
    "edit the image — add a sun in the top-right corner",
    "regenerate it but with a navy palette",
    "redesign the logo, more curved",
    "another variation of this image please",
    "make the previous image more cinematic",
    "change the image background to white",
    "remove the watermark from this image",
]


def test_user_repro_no_longer_misroutes():
    """Exact user reproduction — verbose website-strategy email full of
    image words must NOT be classified as an image edit."""
    assert _quick_image_edit_intent(USER_REPRO_MESSAGE, True, strict=True) is False


def test_strict_mode_vetoes_verbose_and_doc_intent():
    for prompt in NEGATIVE_PROMPTS:
        assert _quick_image_edit_intent(prompt, True, strict=True) is False, (
            f"Strict mode should NOT fire on: {prompt[:80]!r}"
        )


def test_strict_mode_keeps_legit_image_edits():
    for prompt in POSITIVE_PROMPTS:
        assert _quick_image_edit_intent(prompt, True, strict=True) is True, (
            f"Strict mode should still fire on legit edit: {prompt!r}"
        )


def test_non_strict_mode_unchanged():
    """Upload-handler path (where the user actually attached an image)
    keeps the old permissive behavior so single-word edits like 'darker'
    still work when there's a real upload."""
    # No anaphora, no edit-verb either — should be False even in
    # non-strict because there's nothing to act on.
    assert _quick_image_edit_intent("hello there", True, strict=False) is False
    # Edit verb only — non-strict True (legacy behavior).
    assert _quick_image_edit_intent("make it darker", True, strict=False) is True
    # Strict mode would veto "make it darker" only if it lacked an
    # anaphoric image reference. Here the pronoun "it" is generic — but
    # we explicitly allow "darker" through with non-strict.


def test_has_image_false_always_returns_false():
    """If no image is present (and we're not in strict-with-conv-history
    mode), no edit intent regardless of phrasing."""
    assert _quick_image_edit_intent("edit this image", False, strict=False) is False
    assert _quick_image_edit_intent("edit this image", False, strict=True) is False
