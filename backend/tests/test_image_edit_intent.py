"""Regression: when a user uploads an image AND asks for a derived/new image,
the upload endpoint must take the Nano Banana edit/compose fast path —
NOT the vision-describe path that just narrates what the image shows.

This locks the behaviour the user reported on Feb 5, 2026: uploading an
image and saying "generate a 160x600 banner based on this" was producing a
verbose description + "Would you like me to generate this for you now?"
instead of just attaching the new image.
"""
import importlib
import sys

import pytest


@pytest.fixture(scope="module")
def server_module():
    sys.modules.pop("server", None)
    return importlib.import_module("server")


# ── Should be classified as edit (image attached + creation/derivation prompt)
EDIT_PROMPTS = [
    # Narrow edit verbs — pre-existing behaviour, must still pass
    "Add the letter B to the lower right corner",
    "Remove the background",
    "Make it more vibrant",
    "Change the sky to sunset",
    "Resize this to 1080x1080",
    # Creation verbs + image keyword — NEW behaviour
    "Make me a 160x600 banner version of this image",
    "Create a 160x600 vertical banner from this",
    "Generate a new image based on this attached image",
    "Design an Instagram post using this photo",
    "Render a poster from this image",
    "Produce a hero image variation",
    "Draft me a LinkedIn cover from this",
    "Build a thumbnail using this graphic",
    # Derivation phrases — NEW behaviour
    "Something inspired by this",
    "A version of this but in dark mode",
    "Redesign this for mobile",
    "Reimagine this with neon colors",
    "Recreate this in a vector style",
    "Remake this in a 9:16 format",
]

# ── Should fall through to vision-describe (no edit intent)
DESCRIBE_PROMPTS = [
    "Describe this image",
    "What does this show?",
    "Summarize what you see in this picture",
    "What is in here?",
    "Tell me about this image",
    "Caption this for me",  # caption is description, not new image
    "What text is on this poster?",
]


@pytest.mark.parametrize("prompt", EDIT_PROMPTS)
def test_edit_intent_true(server_module, prompt):
    assert server_module._quick_image_edit_intent(prompt, True) is True, (
        f"Expected EDIT intent for: {prompt!r}"
    )


@pytest.mark.parametrize("prompt", DESCRIBE_PROMPTS)
def test_describe_intent_false(server_module, prompt):
    assert server_module._quick_image_edit_intent(prompt, True) is False, (
        f"Expected DESCRIBE intent (False) for: {prompt!r}"
    )


def test_no_image_always_false(server_module):
    """Without an image attached, the function must always return False."""
    for prompt in EDIT_PROMPTS + DESCRIBE_PROMPTS:
        assert server_module._quick_image_edit_intent(prompt, False) is False


def test_empty_text_false(server_module):
    assert server_module._quick_image_edit_intent("", True) is False
    assert server_module._quick_image_edit_intent(None, True) is False
